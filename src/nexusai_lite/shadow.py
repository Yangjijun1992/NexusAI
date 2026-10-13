"""影子模式评估（S4-03/S4-04，reference F8 / §8 阶段 4）。

影子模式**只记录**调度器「将会如何选择」的建议（写入 ``shadow_recommendations``），
**绝不改写 ``tasks`` 或真实 ``schedule_decisions``**，因此预测/建议在通过离线回测、
影子观察与人工审核前不会影响真实调度。

数据边界与安全约束：
- 建议基于本工具自身的窗口规则与自身观测，**不声称**预测供应商内部负载或全局流量。
- 建议/advisory 记录永远**不能**覆盖安全/熔断/预算硬上限——本模块不触碰这些门限，
  ``recommend`` 只产生只读的建议记录。
- S4-04：策略调整经 ``config.propose_config(..., human_approval=True)`` 生成为
  ``proposed`` 版本；**激活始终是人工 CLI 步骤**（本模块绝不调用
  ``activate_config``，也不修改 budget 等受保护项）。
"""

# ruff: noqa: S608
# S608: list_recommendations 的 WHERE 子句仅由固定字面串拼接（无参数插值），
# 所有值（since/until/limit）均通过占位符参数化，非注入向量。

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from nexusai_lite.collector.window_rules import get_active_window_rule, is_peak
from nexusai_lite.config import _PROTECTED_KEYS, _diff_json, get_active_config, propose_config
from nexusai_lite.tasks import TaskService, TaskStatus

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Mapping

#: 距离截止时间多久内视为「重要任务」保护窗口（分钟，与调度器一致）。
_URGENT_DEADLINE_MINUTES = 60
#: 评估报告的命中率阈值：达到则判为 effective。
_HIT_RATE_THRESHOLD = 0.6
#: 评估的最小可判定样本数；低于则判为 insufficient_data。
_MIN_EVAL_SAMPLES = 10
#: 生成策略更新提案所需的最小评估样本数（证据充分才提案）。
_MIN_PROPOSE_SAMPLES = 20
#: 学习到的高峰小时数（从自身观测导出）。
_MAX_PEAK_HOURS = 3
#: 可作为 1 小时高峰窗口起点的最晚整点小时（23 点无法合法收尾）。
_MAX_PEAK_HOUR_VALUE = 22

_ACTION_ALLOW = "ALLOW"
_ACTION_DELAY = "DELAY"

_REASON_DELAY = "shadow_peak_defer"
_REASON_ALLOW = "shadow_off_peak"
_REASON_IMPORTANT = "important_task"


@dataclass(frozen=True)
class ShadowRecommendation:
    """一条影子模式建议（advisory，只记录不生效）。

    Attributes:
        recommendation_id: 建议唯一 ID。
        task_id: 关联任务 ID（可能为 None）。
        recommended_action: ``ALLOW`` 或 ``DELAY``。
        reason_codes: 建议原因代码元组。
        predicted_metric: 关联的预测指标值（可选）。
        model: 生成该建议的模型/规则名。
        created_at: 记录时间（UTC ISO 字符串）。
    """

    recommendation_id: str
    task_id: str | None
    recommended_action: str
    reason_codes: tuple[str, ...]
    predicted_metric: float | None
    model: str
    created_at: str


@dataclass(frozen=True)
class ShadowEvaluation:
    """一次影子模式效果评估报告（S4-03 交付物）。

    Attributes:
        window_days: 评估窗口天数。
        total: 窗口内建议总数。
        delay_recommended/allow_recommended: 各动作建议数。
        sample_count: 有可判定结果、参与命中率计算的建议数。
        hit_rate: 建议与实际观测的吻合率；无样本为 None。
        data_quality: ``"ok"`` 或 ``"INSUFFICIENT_SAMPLE"``。
        verdict: ``"effective"`` / ``"ineffective"`` / ``"insufficient_data"``。
        peak_hours: 从自身观测导出的高峰本地小时（``"HH:00"``）。
    """

    window_days: int
    total: int
    delay_recommended: int
    allow_recommended: int
    sample_count: int
    hit_rate: float | None
    data_quality: str
    verdict: str
    peak_hours: tuple[str, ...]


class ShadowMode:
    """影子模式：只记录调度建议，不控制真实调度。

    Args:
        conn: 已初始化的 SQLite 连接（须含 008 迁移）。
        now_fn: 可注入时钟（返回 aware UTC datetime）；测试用，默认当前 UTC。
    """

    def __init__(  # noqa: D107 - 构造参数语义见类 docstring
        self,
        conn: sqlite3.Connection,
        *,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        self._tasks = TaskService(conn)

    def recommend(self, now: datetime | None = None) -> list[ShadowRecommendation]:
        """计算调度器当前**将会**如何选择，**只记录**到影子表，返回建议列表。

        只读 ``tasks`` / ``window_rules``，**不写入** ``tasks`` 或
        ``schedule_decisions``。重要任务（高优先级/临近截止时间）即使在高峰也建议
        ALLOW；其余按官方窗口规则建议 DELAY（高峰）/ALLOW（空闲）。

        Args:
            now: 参考当前时刻；省略用注入时钟。

        Returns:
            本次生成并已记录的建议列表。
        """
        current = now if now is not None else self._now_fn()
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        now_iso = current.isoformat()
        rule = get_active_window_rule(self._conn)
        peak = is_peak(current, rule)

        recommendations: list[ShadowRecommendation] = []
        for task in self._tasks.list_tasks(TaskStatus.QUEUED):
            urgent = task.priority == "high" or self._deadline_urgent(task.deadline_at, current)
            if urgent:
                action = _ACTION_ALLOW
                reasons = (_REASON_IMPORTANT,)
            elif peak is True:
                action = _ACTION_DELAY
                reasons = (_REASON_DELAY,)
            else:
                action = _ACTION_ALLOW
                reasons = (_REASON_ALLOW,)
            rec = ShadowRecommendation(
                recommendation_id=uuid.uuid4().hex,
                task_id=task.task_id,
                recommended_action=action,
                reason_codes=reasons,
                predicted_metric=None,
                model="shadow-window-rule",
                created_at=now_iso,
            )
            self.record(rec)
            recommendations.append(rec)
        return recommendations

    def record(self, rec: ShadowRecommendation) -> None:
        """持久化一条建议到 ``shadow_recommendations``（单行插入，原子提交）。"""
        self._conn.execute(
            "INSERT INTO shadow_recommendations "
            "(recommendation_id, task_id, recommended_action, reason_codes, "
            " predicted_metric, model, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                rec.recommendation_id,
                rec.task_id,
                rec.recommended_action,
                json.dumps(list(rec.reason_codes), sort_keys=True),
                rec.predicted_metric,
                rec.model,
                rec.created_at,
            ),
        )

    def list_recommendations(
        self,
        *,
        since: str | None = None,
        until: str | None = None,
        limit: int = 100,
    ) -> list[ShadowRecommendation]:
        """列出影子建议（可选按 ``created_at`` 过滤，按创建时间升序有限制）。"""
        where: list[str] = []
        params: list[object] = []
        if since is not None:
            where.append("created_at >= ?")
            params.append(since)
        if until is not None:
            where.append("created_at < ?")
            params.append(until)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        sql = (
            "SELECT recommendation_id, task_id, recommended_action, reason_codes, "
            " predicted_metric, model, created_at FROM shadow_recommendations"
            + clause
            + " ORDER BY created_at LIMIT ?"
        )
        rows = self._conn.execute(sql, (*params, limit)).fetchall()
        return [
            ShadowRecommendation(
                recommendation_id=row["recommendation_id"],
                task_id=row["task_id"],
                recommended_action=row["recommended_action"],
                reason_codes=(
                    tuple(json.loads(row["reason_codes"]))
                    if row["reason_codes"]
                    else ()
                ),
                predicted_metric=row["predicted_metric"],
                model=row["model"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def evaluate(self, *, days: int = 30) -> ShadowEvaluation:
        """对比窗口内建议与实际自身观测，生成效果报告。

        把``api_request_logs``聚合为本地小时桶，以每「星期×小时」的中位数为
        预期基准；建议 DELAY 且实际负载高于基准、或建议 ALLOW 且实际未高于基准，
        记为命中。同时导出自身观测中最常处于高峰的本地小时。

        Args:
            days: 评估窗口天数。

        Returns:
            效果评估报告 :class:`ShadowEvaluation`。
        """
        now = self._now_fn()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        since = now - timedelta(days=days)
        config = get_active_config(self._conn)
        tz = ZoneInfo(config.windows.timezone)

        recommendations = self.list_recommendations(since=since.isoformat())
        total = len(recommendations)
        delay_count = sum(1 for r in recommendations if r.recommended_action == _ACTION_DELAY)
        allow_count = total - delay_count

        hourly = _hourly_counts(
            self._conn,
            timezone=config.windows.timezone,
            since=since,
            until=now,
        )
        thresholds = _seasonal_thresholds(hourly)

        hits = 0
        evaluated = 0
        for rec in recommendations:
            bucket_iso = _bucket_iso(rec.created_at, tz)
            if bucket_iso is None:
                continue
            actual = hourly.get(bucket_iso, 0.0)
            threshold = thresholds.get(_weekday_hour(bucket_iso), 0.0)
            elevated = actual > threshold
            correct = elevated if rec.recommended_action == _ACTION_DELAY else not elevated
            evaluated += 1
            if correct:
                hits += 1

        hit_rate: float | None = (hits / evaluated) if evaluated else None
        if evaluated < _MIN_EVAL_SAMPLES:
            data_quality = "INSUFFICIENT_SAMPLE"
            verdict = "insufficient_data"
        elif hit_rate is not None and hit_rate >= _HIT_RATE_THRESHOLD:
            data_quality = "ok"
            verdict = "effective"
        else:
            data_quality = "ok"
            verdict = "ineffective"

        return ShadowEvaluation(
            window_days=days,
            total=total,
            delay_recommended=delay_count,
            allow_recommended=allow_count,
            sample_count=evaluated,
            hit_rate=hit_rate,
            data_quality=data_quality,
            verdict=verdict,
            peak_hours=_peak_hours(hourly),
        )

    def _deadline_urgent(self, deadline_at: str | None, now: datetime) -> bool:
        """判断截止时间是否落在未来 60 分钟内（重要任务保护）。"""
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


def propose_policy_update(
    conn: sqlite3.Connection,
    *,
    evidence: ShadowEvaluation,
    change_reason: str,
) -> int | None:
    """S4-04 策略调整闭环：依据通过评估的证据生成**配置变更提案**。

    仅在证据通过影子评估门限（``verdict == "effective"`` 且样本充足）时，把学习到
    的高峰时段并入 ``windows.peak_windows``，经 ``propose_config(..., human_approval=
    True)`` 生成为 ``proposed`` 版本并返回版本号；否则返回 ``None`` 且不产生提案。

    关键约束：
    - 生成的只是 **proposed** 提案，**激活仍是人工 CLI 步骤**（本函数绝不激活）。
    - 不修改 budget 等受保护配置项（不触碰安全/预算硬上限）。
    - 未通过评估门限的调整不产生任何提案，因此不控制真实调度。

    Args:
        conn: 数据库连接。
        evidence: 影子评估证据。
        change_reason: 变更原因（写入提案审计）。

    Returns:
        新提案版本号，或未通过门限时为 None。
    """
    if evidence.verdict != "effective" or evidence.sample_count < _MIN_PROPOSE_SAMPLES:
        return None

    # 基于当前生效配置构建提案（保留既有 section），仅改写 windows.peak_windows。
    active = get_active_config(conn)
    payload = active.model_dump()
    current = [list(w) for w in payload["windows"]["peak_windows"]]
    for hour in evidence.peak_hours:
        window = _hour_window(hour)
        if window is not None and window not in current:
            current.append(window)
    # 按开始时间稳定排序去重。
    current = sorted({tuple(w) for w in current})
    payload["windows"]["peak_windows"] = [list(w) for w in current]

    # 防御纵深：即使 human_approval=True，也只允许触碰 windows.peak_windows；
    # 任何受保护配置项（预算/沙箱/白名单/并发上限）都不允许出现在提案差异中。
    diff = _diff_json(active.model_dump(), payload)
    protected = [
        path
        for path in diff
        if any(path == key or path.startswith(f"{key}.") for key in _PROTECTED_KEYS)
    ]
    if protected:
        msg = f"策略更新提案试图修改受保护配置项: {protected}"
        raise ValueError(msg)

    return propose_config(
        conn,
        payload,
        change_reason,
        created_by="optimization",
        human_approval=True,
    )


def _hour_window(hour_raw: str) -> list[str] | None:
    """把 ``"HH:00"`` 转为 ``["HH:00", "HH+1:00"]``；23 点无法合法收尾返回 None。"""
    hour_s, minute_s = hour_raw.split(":")
    try:
        hour = int(hour_s)
        minute = int(minute_s)
    except ValueError:
        return None
    if minute != 0 or not 0 <= hour <= _MAX_PEAK_HOUR_VALUE:
        return None
    end_hour = hour + 1
    return [f"{hour:02d}:00", f"{end_hour:02d}:00"]


def _hourly_counts(
    conn: sqlite3.Connection,
    *,
    timezone: str,
    since: datetime,
    until: datetime,
) -> dict[str, float]:
    """把窗口内自身请求聚合为「本地小时桶 ISO → 请求数」映射（空桶记 0 仅当命中）。"""
    tz = ZoneInfo(timezone)
    rows = conn.execute(
        "SELECT started_at FROM api_request_logs WHERE started_at >= ? AND started_at < ?",
        (since.isoformat(), until.isoformat()),
    ).fetchall()
    counts: dict[str, float] = {}
    for row in rows:
        try:
            dt = datetime.fromisoformat(str(row["started_at"]))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        bucket = dt.astimezone(tz).replace(minute=0, second=0, microsecond=0)
        key = bucket.isoformat()
        counts[key] = counts.get(key, 0.0) + 1.0
    return counts


def _bucket_iso(iso: str, tz: ZoneInfo) -> str | None:
    """把 ISO 时刻转为本地整点小时桶的 ISO；解析失败返回 None。"""
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(tz).replace(minute=0, second=0, microsecond=0).isoformat()


def _weekday_hour(bucket_iso: str) -> tuple[int, int]:
    """从本地小时桶 ISO 求「星期×小时」键。"""
    dt = datetime.fromisoformat(bucket_iso)
    return dt.isoweekday(), dt.hour


def _seasonal_thresholds(hourly: Mapping[str, float]) -> dict[tuple[int, int], float]:
    """按「星期×小时」计算每组的请求数中位数作为预期基准。"""
    grouped: dict[tuple[int, int], list[float]] = {}
    for bucket_iso, value in hourly.items():
        grouped.setdefault(_weekday_hour(bucket_iso), []).append(value)
    thresholds: dict[tuple[int, int], float] = {}
    for key, values in grouped.items():
        ordered = sorted(values)
        thresholds[key] = ordered[len(ordered) // 2]
    return thresholds


def _peak_hours(hourly: Mapping[str, float]) -> tuple[str, ...]:
    """从自身观测导出请求量最高的本地小时（``"HH:00"``），按量降序截断。"""
    by_hour: dict[str, float] = {}
    for bucket_iso, value in hourly.items():
        hour = bucket_iso[11:16]  # "T09:00" → "09:00"
        by_hour[hour] = by_hour.get(hour, 0.0) + value
    ordered = sorted(by_hour.items(), key=lambda kv: (-kv[1], kv[0]))
    return tuple(hour for hour, _ in ordered[:_MAX_PEAK_HOURS])
