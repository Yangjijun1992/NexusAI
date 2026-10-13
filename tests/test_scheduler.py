"""错峰调度器与决策记录测试（S2-06）。

使用临时 SQLite 数据库与可注入时钟，确定性验证决策优先级链、错峰规则应用、
预算/熔断/容量拦截、``schedule_cycle`` 记录与 ``explain`` 解释、以及决策记录
不保存密钥/Prompt（reference F4、§6.4、§8 阶段 2 退出条件）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from nexusai_lite import db
from nexusai_lite.budget import BudgetGuard
from nexusai_lite.collector.window_rules import WindowRuleData, upsert_window_rule
from nexusai_lite.scheduler import (
    REASON_AT_CAPACITY,
    REASON_BUDGET_EXCEEDED,
    REASON_CIRCUIT_OPEN,
    REASON_CONSERVATIVE_CAP,
    REASON_IMPORTANT,
    REASON_NO_WINDOW_RULE,
    REASON_NOT_QUEUED_ELIGIBLE,
    REASON_OFF_PEAK,
    REASON_PEAK_DEFER,
    REASON_TASK_NOT_FOUND,
    DecisionResult,
    Scheduler,
)
from nexusai_lite.tasks import TaskService

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path


# 2026-10-12 为周一；北京时间 = UTC+8。
# 高峰刻点：UTC 02:00 = 北京 10:00（09:00-12:00 高峰内）。
_PEAK_UTC = datetime(2026, 10, 12, 2, 0, 0, tzinfo=UTC)
# 空闲刻点：UTC 04:30 = 北京 12:30（两高峰窗口之间）。
_OFF_PEAK_UTC = datetime(2026, 10, 12, 4, 30, 0, tzinfo=UTC)

_DEFAULT_RULE = WindowRuleData(
    timezone="Asia/Shanghai",
    workdays=(1, 2, 3, 4, 5),
    peak_windows=(("09:00", "12:00"), ("14:00", "18:00")),
    source="official-pricing-page",
)


class _Clock:
    """可推进的可注入时钟（返回 aware UTC datetime）。"""

    def __init__(self, start: datetime) -> None:
        """初始化时钟到给定起点。"""
        self._now = start

    def advance(self, seconds: float) -> None:
        """把时钟向前推进给定秒数。"""
        self._now += timedelta(seconds=seconds)

    def __call__(self) -> datetime:
        """返回当前时间。"""
        return self._now


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """返回已初始化的临时 SQLite 连接。"""
    db_path_ = tmp_path / "nexusai.db"
    db.init_db(db_path_)
    c = db.connect(db_path_)
    yield c
    c.close()


def _seed_rule(conn: sqlite3.Connection) -> None:
    """写入默认高峰窗口规则并激活。"""
    upsert_window_rule(conn, _DEFAULT_RULE, source="official-pricing-page")


def _make_scheduler(
    conn: sqlite3.Connection,
    clock: _Clock,
    *,
    max_inflight: int = 2,
    conservative_inflight: int = 1,
) -> Scheduler:
    """构造注入时钟的调度器。"""
    return Scheduler(
        conn,
        now_fn=clock,
        max_inflight=max_inflight,
        conservative_inflight=conservative_inflight,
    )


def _queued_task(
    service: TaskService,
    *,
    task_type: str = "llm",
    priority: str = "normal",
    estimated_cost: float | None = None,
    deadline_at: str | None = None,
) -> str:
    """创建并排入 QUEUED 的任务，返回 task_id。"""
    task = service.create_task(
        task_type=task_type,
        priority=priority,
        estimated_cost=estimated_cost,
        deadline_at=deadline_at,
    )
    service.queue(task.task_id)
    return task.task_id


# ---------------------------------------------------------------- 错峰

def test_peak_non_urgent_delays(conn: sqlite3.Connection) -> None:
    """高峰时段非紧急任务应 DELAY（peak_defer）。"""
    _seed_rule(conn)
    clock = _Clock(_PEAK_UTC)
    service = TaskService(conn)
    task_id = _queued_task(service)
    decision = _make_scheduler(conn, clock).decide(task_id)

    assert decision.result is DecisionResult.DELAY
    assert REASON_PEAK_DEFER in decision.reason_codes
    assert decision.data_quality == "ok"


def test_peak_urgent_allows(conn: sqlite3.Connection) -> None:
    """高峰时段高优先级任务应受保护 ALLOW（important_task）。"""
    _seed_rule(conn)
    clock = _Clock(_PEAK_UTC)
    service = TaskService(conn)
    task_id = _queued_task(service, priority="high")
    decision = _make_scheduler(conn, clock).decide(task_id)

    assert decision.result is DecisionResult.ALLOW
    assert REASON_IMPORTANT in decision.reason_codes


def test_peak_deadline_urgent_allows(conn: sqlite3.Connection) -> None:
    """高峰时段临近截止时间的任务应受保护 ALLOW（important_task）。"""
    _seed_rule(conn)
    clock = _Clock(_PEAK_UTC)
    service = TaskService(conn)
    deadline = (_PEAK_UTC + timedelta(minutes=30)).isoformat()
    task_id = _queued_task(service, deadline_at=deadline)
    decision = _make_scheduler(conn, clock).decide(task_id)

    assert decision.result is DecisionResult.ALLOW
    assert REASON_IMPORTANT in decision.reason_codes


def test_off_peak_allows(conn: sqlite3.Connection) -> None:
    """空闲时段非紧急任务应 ALLOW（off_peak）。"""
    _seed_rule(conn)
    clock = _Clock(_OFF_PEAK_UTC)
    service = TaskService(conn)
    task_id = _queued_task(service)
    decision = _make_scheduler(conn, clock).decide(task_id)

    assert decision.result is DecisionResult.ALLOW
    assert REASON_OFF_PEAK in decision.reason_codes


def test_no_rule_conservative_allow(conn: sqlite3.Connection) -> None:
    """无活跃时段规则时按保守策略放行：ALLOW no_window_rule + NO_RULE。"""
    clock = _Clock(_OFF_PEAK_UTC)
    service = TaskService(conn)
    task_id = _queued_task(service)
    decision = _make_scheduler(conn, clock).decide(task_id)

    assert decision.result is DecisionResult.ALLOW
    assert REASON_NO_WINDOW_RULE in decision.reason_codes
    assert REASON_CONSERVATIVE_CAP in decision.reason_codes
    assert decision.data_quality == "NO_RULE"


# ---------------------------------------------------------------- 熔断

def test_breaker_open_model_delays(conn: sqlite3.Connection) -> None:
    """model 维度熔断 OPEN 时应 DELAY（circuit_open）。"""
    service = TaskService(conn)
    task_id = _queued_task(service)
    # 直接播种 model 维度 OPEN（opened_at 置 NULL 避免冷却自动转 HALF_OPEN）。
    conn.execute(
        "INSERT INTO circuit_breakers "
        "(dimension, state, failure_count, success_count, in_flight_probes, "
        " opened_at, last_change_at) VALUES (?, 'open', 0, 0, 0, NULL, ?)",
        ("model:deepseek-chat", datetime.now(UTC).isoformat()),
    )
    clock = _Clock(_OFF_PEAK_UTC)
    decision = _make_scheduler(conn, clock).decide(task_id)

    assert decision.result is DecisionResult.DELAY
    assert REASON_CIRCUIT_OPEN in decision.reason_codes


def test_breaker_open_provider_delays(conn: sqlite3.Connection) -> None:
    """provider 维度熔断 OPEN 时应 DELAY（circuit_open）优先于 model。"""
    service = TaskService(conn)
    task_id = _queued_task(service)
    conn.execute(
        "INSERT INTO circuit_breakers "
        "(dimension, state, failure_count, success_count, in_flight_probes, "
        " opened_at, last_change_at) VALUES (?, 'open', 0, 0, 0, NULL, ?)",
        ("provider:deepseek", datetime.now(UTC).isoformat()),
    )
    clock = _Clock(_OFF_PEAK_UTC)
    decision = _make_scheduler(conn, clock).decide(task_id)

    assert decision.result is DecisionResult.DELAY
    assert REASON_CIRCUIT_OPEN in decision.reason_codes
    assert decision.detail["breaker_dimension"] == "provider:deepseek"


# ---------------------------------------------------------------- 预算

def test_budget_exceeded_rejects(conn: sqlite3.Connection) -> None:
    """预算硬上限超限应 REJECT（budget_exceeded）。"""
    service = TaskService(conn)
    task_id = _queued_task(service, estimated_cost=3.0)
    # task scope 硬上限 1.0 < 估算 3.0 → 超限。
    BudgetGuard(conn).set_limits(f"task:{task_id}", hard_limit=1.0)
    clock = _Clock(_OFF_PEAK_UTC)
    decision = _make_scheduler(conn, clock).decide(task_id)

    assert decision.result is DecisionResult.REJECT
    assert REASON_BUDGET_EXCEEDED in decision.reason_codes


def test_platform_daily_limit_exceeded_rejects(conn: sqlite3.Connection) -> None:
    """平台（每日）预算硬上限已满 → REJECT（budget_exceeded），尊重日上限。"""
    service = TaskService(conn)
    task_id = _queued_task(service, estimated_cost=3.0)
    # platform 日上限 2.0 < 估算 3.0 → 超限。
    BudgetGuard(conn).set_limits("platform", hard_limit=2.0)
    clock = _Clock(_OFF_PEAK_UTC)
    decision = _make_scheduler(conn, clock).decide(task_id)

    assert decision.result is DecisionResult.REJECT
    assert REASON_BUDGET_EXCEEDED in decision.reason_codes
    assert decision.detail["budget_scope"] == "platform"


# ---------------------------------------------------------------- 容量

def test_at_capacity_delays(conn: sqlite3.Connection) -> None:
    """自身 RUNNING 并发达上限时应 DELAY（at_capacity）。"""
    _seed_rule(conn)
    clock = _Clock(_OFF_PEAK_UTC)
    service = TaskService(conn)
    for _ in range(2):  # max_inflight=2
        tid = _queued_task(service)
        service.start(tid)
    queued_id = _queued_task(service)
    decision = _make_scheduler(conn, clock, max_inflight=2).decide(queued_id)

    assert decision.result is DecisionResult.DELAY
    assert REASON_AT_CAPACITY in decision.reason_codes
    assert decision.detail["running_count"] == 2
    assert decision.detail["cap"] == 2


# ---------------------------------------------------------------- 资格

def test_non_queued_rejects(conn: sqlite3.Connection) -> None:
    """非 QUEUED 任务（如 PAUSED）应 REJECT（not_queued_eligible）。"""
    service = TaskService(conn)
    task = service.create_task(task_type="llm")
    service.queue(task.task_id)
    service.pause(task.task_id)
    clock = _Clock(_OFF_PEAK_UTC)
    decision = _make_scheduler(conn, clock).decide(task.task_id)

    assert decision.result is DecisionResult.REJECT
    assert REASON_NOT_QUEUED_ELIGIBLE in decision.reason_codes


def test_missing_task_rejects(conn: sqlite3.Connection) -> None:
    """不存在的任务应 REJECT（task_not_found）。"""
    clock = _Clock(_OFF_PEAK_UTC)
    decision = _make_scheduler(conn, clock).decide("no-such-task")

    assert decision.result is DecisionResult.REJECT
    assert REASON_TASK_NOT_FOUND in decision.reason_codes


# ---------------------------------------------------------------- 记录与解释

def test_schedule_cycle_records_and_explain_roundtrip(conn: sqlite3.Connection) -> None:
    """schedule_cycle 写入 schedule_decisions，explain 可读回。"""
    _seed_rule(conn)
    clock = _Clock(_OFF_PEAK_UTC)
    service = TaskService(conn)
    task_id = _queued_task(service)

    scheduler = _make_scheduler(conn, clock)
    decisions = scheduler.schedule_cycle()

    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.task_id == task_id

    row = conn.execute(
        "SELECT policy_version, data_quality, reason_codes, metrics_summary "
        "FROM schedule_decisions WHERE decision_id=?",
        (decision.decision_id,),
    ).fetchone()
    assert row is not None
    assert row["policy_version"] == "1"
    assert row["data_quality"] == "ok"
    reason_codes = json.loads(row["reason_codes"])
    assert REASON_OFF_PEAK in reason_codes

    explained = scheduler.explain(decision.decision_id)
    assert explained["decision_id"] == decision.decision_id
    assert explained["task_id"] == task_id
    assert explained["decision"] == DecisionResult.ALLOW.value
    assert REASON_OFF_PEAK in explained["reason_codes"]
    assert explained["policy_version"] == "1"


def test_explain_missing_returns_empty(conn: sqlite3.Connection) -> None:
    """explain 不存在的决策返回空 dict。"""
    scheduler = _make_scheduler(conn, _Clock(_OFF_PEAK_UTC))
    assert scheduler.explain("nope") == {}


def test_records_contain_no_secrets(conn: sqlite3.Connection) -> None:
    """决策记录不得包含密钥/Prompt 等敏感内容。"""
    _seed_rule(conn)
    clock = _Clock(_OFF_PEAK_UTC)
    service = TaskService(conn)
    marker = "NONRECORDED-726391"
    _queued_task(service, task_type=marker)

    scheduler = _make_scheduler(conn, clock)
    decision = scheduler.schedule_cycle()[0]
    row = conn.execute(
        "SELECT reason_codes, metrics_summary, task_id FROM schedule_decisions "
        "WHERE decision_id=?",
        (decision.decision_id,),
    ).fetchone()

    blob = (row["reason_codes"] or "") + (row["metrics_summary"] or "") + row["task_id"]
    assert marker not in blob
    assert "prompt" not in blob.lower()
