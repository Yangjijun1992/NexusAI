"""错峰调度器与决策记录（S2-06）。

实现 reference.md F4 / §6.4 的调度决策优先级链（安全授权 > 预算硬上限 > 实时
服务健康/熔断 > 自身资源容量 > 任务优先级/截止时间 > 官方时段规则与自身观测），
并在每次决策时把结果、原因代码、关键输入摘要、样本数、数据质量、规则版本写入
``schedule_decisions`` 供审计与解释（§8 阶段 2 退出条件）。

本模块是**只读调度器**：不修改任务状态，由消费者按 ``ALLOW`` 决策实际取走任务
执行；``schedule_cycle`` 仅对 ``QUEUED`` 任务做 decide + record。

安全：决策记录只保存原因代码与小型度量（metrics_summary），绝不保存完整密钥、
完整 Prompt 或任何不必要的个人信息。
"""

from __future__ import annotations

import enum
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from nexusai_lite.budget import BudgetGuard
from nexusai_lite.circuit_breaker import BreakerState, CircuitBreaker
from nexusai_lite.collector.window_rules import get_active_window_rule, is_peak
from nexusai_lite.config import get_active_config
from nexusai_lite.tasks import Task, TaskService, TaskStatus

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Mapping

    from nexusai_lite.collector.window_rules import WindowRuleData

# ---------------------------------------------------------------- 原因代码

#: 任务不存在（无法调度）。
REASON_TASK_NOT_FOUND = "task_not_found"
#: 任务不在 ``QUEUED`` 状态，不具备调度资格。
REASON_NOT_QUEUED_ELIGIBLE = "not_queued_eligible"
#: 预算硬上限已用满，拒绝新增可计费执行。
REASON_BUDGET_EXCEEDED = "budget_exceeded"
#: 服务熔断打开（provider/model 维度为 OPEN），延迟非紧急任务。
REASON_CIRCUIT_OPEN = "circuit_open"
#: 熔断半开（HALF_OPEN）：仅允许有限探测，调度器不得批量放行，延迟非紧急任务。
REASON_CIRCUIT_HALF_OPEN = "circuit_half_open"
#: 自身并发容量已达上限。
REASON_AT_CAPACITY = "at_capacity"
#: 无活跃时段规则时采用保守并发上限（不推断为低压力）。
REASON_CONSERVATIVE_CAP = "conservative_cap_applied"
#: 重要任务（高优先级或临近截止时间），即使高峰也受保护。
REASON_IMPORTANT = "important_task"
#: 官方时段规则判定处于高峰计价时段，非紧急任务延迟。
REASON_PEAK_DEFER = "peak_defer"
#: 官方时段规则判定处于空闲时段。
REASON_OFF_PEAK = "off_peak"
#: 无活跃时段规则（data_quality="NO_RULE"），按保守策略放行。
REASON_NO_WINDOW_RULE = "no_window_rule"
#: provider 维度不可用，跳过该熔断维度检查。
REASON_PROVIDER_SKIPPED = "provider_dimension_skipped"
#: model 维度不可用，跳过该熔断维度检查。
REASON_MODEL_SKIPPED = "model_dimension_skipped"

#: 判断「临近截止时间」的时间窗（分钟）。
_URGENT_DEADLINE_MINUTES = 60
_TASK_SCOPE_PREFIX = "task:"
_DEFAULT_PROVIDER = "deepseek"
#: 任务类型到 provider 的映射；未知任务类型回退 ``_DEFAULT_PROVIDER``。
_TASK_TYPE_PROVIDER: dict[str, str] = {"llm": _DEFAULT_PROVIDER}


class DecisionResult(str, enum.Enum):  # noqa: UP042 - 持久化要求 str+Enum 便于存取
    """调度决策结果（reference §6.4）。"""

    ALLOW = "allow"
    DELAY = "delay"
    PAUSE = "pause"
    REJECT = "reject"
    REVIEW_REQUIRED = "review_required"


@dataclass(frozen=True, slots=True)
class Decision:
    """一次调度决策的可审计记录。"""

    decision_id: str
    task_id: str
    result: DecisionResult
    reason_codes: tuple[str, ...]
    policy_version: str | None
    data_quality: str
    sample_count: int
    decided_at: str
    detail: dict[str, Any] = field(default_factory=dict)


class Scheduler:
    """错峰调度器：按决策优先级链评估任务并记录决策。

    Args:
        conn: 已初始化的 SQLite 连接（须含 001/004/005 迁移）。
        now_fn: 可注入时钟（返回 aware UTC datetime）；测试用，默认
            ``datetime.now(UTC)``。
        max_inflight: 自身最大并发任务数（有活跃时段规则时的容量上限）。
        conservative_inflight: 无活跃时段规则时的保守并发上限（reference：
            数据质量不足时采用保守策略，不得默认判定为低压力）。
    """

    def __init__(  # noqa: D107 - 构造参数语义见类 docstring
        self,
        conn: sqlite3.Connection,
        *,
        now_fn: Callable[[], datetime] | None = None,
        max_inflight: int = 2,
        conservative_inflight: int = 1,
    ) -> None:
        self._conn = conn
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        self._max_inflight = max_inflight
        self._conservative_inflight = conservative_inflight
        self._tasks = TaskService(conn)
        self._budget = BudgetGuard(conn)
        self._breaker = CircuitBreaker(conn, now_fn=self._now_fn)

    # ------------------------------------------------------------- 决策链

    def decide(  # noqa: C901, PLR0911, PLR0912 - 决策链按 §6.4 顺序多级阻断，属规定行为
        self, task_id: str
    ) -> Decision:
        """按优先级链评估任务并返回 :class:`Decision`（只读）。

        链顺序：安全/授权 > 预算硬上限 > 熔断 > 自身容量 > 优先级/截止时间 >
        官方时段规则。首个阻断规则即返回；沿途的非阻断判据积累到 ``reason_codes``。
        """
        decide_id = uuid.uuid4().hex
        now = self._now_fn()
        now_iso = now.isoformat()
        policy_version = self._active_config_version()
        rule = get_active_window_rule(self._conn)
        data_quality = "NO_RULE" if rule is None else "ok"

        task = self._tasks.get_task(task_id)
        if task is None:
            return Decision(
                decision_id=decide_id,
                task_id=task_id,
                result=DecisionResult.REJECT,
                reason_codes=(REASON_TASK_NOT_FOUND,),
                policy_version=policy_version,
                data_quality=data_quality,
                sample_count=self._sample_count(),
                decided_at=now_iso,
                detail=self._detail(rule, {}),
            )

        reason_codes: list[str] = []

        if task.status is not TaskStatus.QUEUED:
            return self._decision(
                decide_id,
                task_id,
                DecisionResult.REJECT,
                (*reason_codes, REASON_NOT_QUEUED_ELIGIBLE),
                policy_version,
                data_quality,
                now_iso,
                rule,
                {},
            )

        provider, model = self._resolve_provider_model(task)

        # 预算硬上限（scope = task:<task_id>）。
        estimated = task.estimated_cost or 0.0
        budget_check = self._budget.check(f"{_TASK_SCOPE_PREFIX}{task_id}", estimated)
        if not budget_check.allowed:
            return self._decision(
                decide_id,
                task_id,
                DecisionResult.REJECT,
                (*reason_codes, REASON_BUDGET_EXCEEDED),
                policy_version,
                data_quality,
                now_iso,
                rule,
                {
                    "budget_scope": f"{_TASK_SCOPE_PREFIX}{task_id}",
                    "budget_would_exceed": budget_check.would_exceed,
                    "estimated_cost": estimated,
                },
            )

        # 平台（每日）预算硬上限（§12 规则 9）：调度也尊重日上限，超限即 REJECT。
        # 先播种 platform 上限（幂等），再只读检查估算是否突破日上限。
        self._budget.from_config(get_active_config(self._conn))
        platform_check = self._budget.check("platform", estimated)
        if not platform_check.allowed:
            return self._decision(
                decide_id,
                task_id,
                DecisionResult.REJECT,
                (*reason_codes, REASON_BUDGET_EXCEEDED),
                policy_version,
                data_quality,
                now_iso,
                rule,
                {
                    "budget_scope": "platform",
                    "budget_would_exceed": platform_check.would_exceed,
                    "estimated_cost": estimated,
                    "budget_remaining": platform_check.remaining,
                },
            )

        # 实时服务健康/熔断（provider / model 维度；只读 state，不消费 HALF_OPEN 探测）。
        if provider is None:
            reason_codes.append(REASON_PROVIDER_SKIPPED)
        else:
            provider_breaker = self._breaker.state(f"provider:{provider}")
            if provider_breaker is not BreakerState.CLOSED:
                reason = (
                    REASON_CIRCUIT_HALF_OPEN
                    if provider_breaker is BreakerState.HALF_OPEN
                    else REASON_CIRCUIT_OPEN
                )
                return self._decision(
                    decide_id,
                    task_id,
                    DecisionResult.DELAY,
                    (*reason_codes, reason),
                    policy_version,
                    data_quality,
                    now_iso,
                    rule,
                    {
                        "provider": provider,
                        "model": model,
                        "breaker_dimension": f"provider:{provider}",
                        "breaker_state": provider_breaker.value,
                    },
                )
        if model is None:
            reason_codes.append(REASON_MODEL_SKIPPED)
        else:
            model_breaker = self._breaker.state(f"model:{model}")
            if model_breaker is not BreakerState.CLOSED:
                reason = (
                    REASON_CIRCUIT_HALF_OPEN
                    if model_breaker is BreakerState.HALF_OPEN
                    else REASON_CIRCUIT_OPEN
                )
                return self._decision(
                    decide_id,
                    task_id,
                    DecisionResult.DELAY,
                    (*reason_codes, reason),
                    policy_version,
                    data_quality,
                    now_iso,
                    rule,
                    {
                        "provider": provider,
                        "model": model,
                        "breaker_dimension": f"model:{model}",
                        "breaker_state": model_breaker.value,
                    },
                )

        # 自身资源容量。
        running_count = self._running_count()
        cap = self._conservative_inflight if rule is None else self._max_inflight
        if rule is None:
            reason_codes.append(REASON_CONSERVATIVE_CAP)
        if running_count >= cap:
            return self._decision(
                decide_id,
                task_id,
                DecisionResult.DELAY,
                (*reason_codes, REASON_AT_CAPACITY),
                policy_version,
                data_quality,
                now_iso,
                rule,
                {
                    "running_count": running_count,
                    "cap": cap,
                    "provider": provider,
                    "model": model,
                },
            )

        # 任务优先级/截止时间（重要任务受保护）。
        urgent = task.priority == "high" or self._deadline_urgent(task.deadline_at, now)
        if urgent:
            return self._decision(
                decide_id,
                task_id,
                DecisionResult.ALLOW,
                (*reason_codes, REASON_IMPORTANT),
                policy_version,
                data_quality,
                now_iso,
                rule,
                {
                    "running_count": running_count,
                    "cap": cap,
                    "provider": provider,
                    "model": model,
                    "urgent": urgent,
                    "priority": task.priority,
                },
            )

        # 官方时段规则（错峰）。
        if is_peak(now, rule):
            return self._decision(
                decide_id,
                task_id,
                DecisionResult.DELAY,
                (*reason_codes, REASON_PEAK_DEFER),
                policy_version,
                data_quality,
                now_iso,
                rule,
                {
                    "running_count": running_count,
                    "cap": cap,
                    "provider": provider,
                    "model": model,
                    "peak": True,
                },
            )
        peak_code = REASON_NO_WINDOW_RULE if rule is None else REASON_OFF_PEAK
        return self._decision(
            decide_id,
            task_id,
            DecisionResult.ALLOW,
            (*reason_codes, peak_code),
            policy_version,
            data_quality,
            now_iso,
            rule,
            {
                "running_count": running_count,
                "cap": cap,
                "provider": provider,
                "model": model,
                "peak": False,
            },
        )

    # ------------------------------------------------------------- 记录

    def record(
        self,
        decision: Decision,
        *,
        manual_override: bool = False,
        override_reason: str | None = None,
    ) -> None:
        """把一次决策写入 ``schedule_decisions``（单条 INSERT，原子提交）。

        Args:
            decision: 要记录的决策。
            manual_override: 是否为人工覆盖决策。
            override_reason: 人工覆盖原因（可选）。

        只写原因代码与小型度量，不写密钥/Prompt。
        """
        self._conn.execute(
            "INSERT INTO schedule_decisions "
            "(decision_id, task_id, decision, reason_codes, metrics_summary, "
            " sample_count, data_quality, policy_version, decided_at, "
            " manual_override, override_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                decision.decision_id,
                decision.task_id,
                decision.result.value,
                json.dumps(list(decision.reason_codes), sort_keys=True),
                json.dumps(decision.detail, sort_keys=True),
                decision.sample_count,
                decision.data_quality,
                decision.policy_version,
                decision.decided_at,
                1 if manual_override else 0,
                override_reason,
            ),
        )

    def schedule_cycle(self, *, limit: int = 100) -> list[Decision]:
        """对至多 ``limit`` 个 ``QUEUED`` 任务依次 decide + record 并返回决策列表。

        只读任务行：不改变任务状态，由消费者按 ``ALLOW`` 取走执行。
        """
        queued = self._tasks.list_tasks(TaskStatus.QUEUED, limit=limit)
        decisions: list[Decision] = []
        for task in queued:
            decision = self.decide(task.task_id)
            self.record(decision)
            decisions.append(decision)
        return decisions

    def explain(self, decision_id: str) -> dict[str, Any]:
        """加载一条已记录决策供审计解释（"决策可解释并定位到规则版本"）。"""
        row = self._conn.execute(
            "SELECT decision_id, task_id, decision, reason_codes, metrics_summary, "
            "       sample_count, data_quality, policy_version, decided_at, "
            "       manual_override, override_reason "
            "FROM schedule_decisions WHERE decision_id=?",
            (decision_id,),
        ).fetchone()
        if row is None:
            return {}
        return {
            "decision_id": row["decision_id"],
            "task_id": row["task_id"],
            "decision": row["decision"],
            "reason_codes": json.loads(row["reason_codes"]) if row["reason_codes"] else [],
            "metrics_summary": json.loads(row["metrics_summary"]) if row["metrics_summary"] else {},
            "sample_count": row["sample_count"],
            "data_quality": row["data_quality"],
            "policy_version": row["policy_version"],
            "decided_at": row["decided_at"],
            "manual_override": bool(row["manual_override"]),
            "override_reason": row["override_reason"],
        }

    # ------------------------------------------------------------- 内部

    def _decision(  # noqa: PLR0913, PLR0917 - 参数与 Decision 核心字段一一对应
        self,
        decision_id: str,
        task_id: str,
        result: DecisionResult,
        reason_codes: tuple[str, ...],
        policy_version: str | None,
        data_quality: str,
        decided_at: str,
        rule: WindowRuleData | None,
        metrics: Mapping[str, object],
    ) -> Decision:
        """构造 :class:`Decision` 并填充小型度量（metrics_summary）。"""
        return Decision(
            decision_id=decision_id,
            task_id=task_id,
            result=result,
            reason_codes=reason_codes,
            policy_version=policy_version,
            data_quality=data_quality,
            sample_count=self._sample_count(),
            decided_at=decided_at,
            detail=self._detail(rule, metrics),
        )

    def _detail(
        self, rule: WindowRuleData | None, metrics: Mapping[str, object]
    ) -> dict[str, object]:
        """合并必选与可选的小型可审计度量；不含任何密钥或 Prompt 内容。"""
        return {"has_window_rule": rule is not None, **metrics}

    def _resolve_provider_model(self, task: Task) -> tuple[str | None, str | None]:
        """解析任务使用的 provider/model。

        provider 由 ``task_type`` 映射得到（未知类型回退 ``deepseek``）；model 取
        当前配置的默认模型。任一解析失败返回对应位置的 ``None`` 以跳过该熔断维度。
        """
        if task.task_type is None:
            return None, None
        provider = _TASK_TYPE_PROVIDER.get(task.task_type, _DEFAULT_PROVIDER)
        model: str | None = None
        try:
            config = get_active_config(self._conn)
            model = config.provider.default_model
        except Exception:  # noqa: BLE001 - 配置解析失败时降级跳过 model 维度
            provider = None
        return provider, model

    def _deadline_urgent(self, deadline_at: str | None, now: datetime) -> bool:
        """判断截止时间是否落在未来 60 分钟内（重要任务受保护）。"""
        if deadline_at is None:
            return False
        try:
            deadline = datetime.fromisoformat(deadline_at)
        except ValueError:
            return False
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        within = timedelta(minutes=_URGENT_DEADLINE_MINUTES)
        return now <= deadline <= now + within

    def _running_count(self) -> int:
        """统计当前 ``RUNNING`` 任务数（自身资源容量）。"""
        row = self._conn.execute(
            "SELECT COUNT(*) AS c FROM tasks WHERE status=?", (TaskStatus.RUNNING.value,)
        ).fetchone()
        return int(row["c"]) if row is not None else 0

    def _sample_count(self) -> int:
        """返回可用的调度样本数（指标输入规模，供可解释性参考）。"""
        cells = self._conn.execute(
            "SELECT COUNT(*) AS c FROM api_request_logs"
        ).fetchone()
        return int(cells["c"]) if cells is not None else 0

    def _active_config_version(self) -> str | None:
        """返回当前活跃配置版本号；无则 None。"""
        row = self._conn.execute(
            "SELECT version FROM config_versions WHERE status='active' "
            "ORDER BY version DESC LIMIT 1"
        ).fetchone()
        return str(row["version"]) if row is not None else None
