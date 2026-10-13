"""夜间/非工作日授权窗口自动启动（S3-01）测试。

使用 ``db.init_db`` / ``db.connect`` 建临时库、确定性注入时钟、无网络。
所有场景围绕验收标准：仅在授权窗口内创建+入队任务；多窗口不产生重复任务；
规则缺失 / 工作日白天 / 非授权夜间一律不启动。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from nexusai_lite import db
from nexusai_lite.collector.window_rules import (
    WindowRuleData,
    upsert_window_rule,
)
from nexusai_lite.nightdev import (
    NightDevPlanner,
    NightDevSpec,
    is_allowed_window,
)

if TYPE_CHECKING:
    import sqlite3

# 默认规则：北京时间周一至周五为工作日（09:00-12:00、14:00-18:00 为高峰，
# 本测试不依赖高峰语义，仅依赖 workdays/timezone）。
_DEFAULT_RULE = WindowRuleData()

_SPECS = (
    NightDevSpec(
        name="refactor-core",
        task_type="nightdev_refactor",
        repo_path=Path("repo-a"),
    ),
    NightDevSpec(name="bump-deps", task_type="nightdev_deps", repo_path=Path("repo-b")),
)


def _conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "nexusai.db"
    db.init_db(db_path)
    return db.connect(db_path)


def _utc(*, year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    """构造带 UTC 时区的给定时刻（与 test_window_rules 一致）。"""
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def _active_rule(conn: sqlite3.Connection, rule: WindowRuleData = _DEFAULT_RULE) -> None:
    upsert_window_rule(conn, rule, source="official-pricing-page")


def _queued_ids(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT task_id FROM tasks WHERE status='QUEUED'"
    ).fetchall()
    return {str(row["task_id"]) for row in rows}


def _task_row(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    assert row is not None
    return row


# ------------------------------------------------------------ is_allowed_window
def test_allowed_night_window_workday_beijing_23h() -> None:
    """北京周二 23:00（工作日，夜间窗口内）→ 允许。"""
    # 2026-10-12 周一在北京 23:00 = UTC 15:00（同日）。
    dt = _utc(year=2026, month=10, day=12, hour=15, minute=0)
    assert is_allowed_window(
        dt, _DEFAULT_RULE, night_start="22:00", night_end="07:00"
    ) is True


def test_not_allowed_workday_daytime_off_peak_13h() -> None:
    """北京周一 13:00（工作日白天，空闲计价时段）→ 不允许。"""
    # 2026-10-12 周一 13:00 = UTC 05:00；虽在高峰窗口之外，仍非授权窗口。
    dt = _utc(year=2026, month=10, day=12, hour=5, minute=0)
    assert is_allowed_window(
        dt, _DEFAULT_RULE, night_start="22:00", night_end="07:00"
    ) is False


def test_allowed_weekend() -> None:
    """北京周六（非工作日）→ 允许，无论时刻。"""
    # 2026-10-17 周六 10:00 = UTC 02:00（同日）。
    dt = _utc(year=2026, month=10, day=17, hour=2, minute=0)
    assert is_allowed_window(
        dt, _DEFAULT_RULE, night_start="22:00", night_end="07:00"
    ) is True


def test_allowed_holiday_workday_daytime() -> None:
    """工作日白天计入 holidays → 允许（假日为非工作日）。"""
    # 2026-10-12 周一 10:00 = UTC 02:00；列为假日故允许。
    dt = _utc(year=2026, month=10, day=12, hour=2, minute=0)
    assert is_allowed_window(
        dt, _DEFAULT_RULE, night_start="22:00", night_end="07:00", holidays=("2026-10-12",)
    ) is True


def test_not_allowed_when_rule_none() -> None:
    """规则缺失 → 保守返回 False（未授权不启动）。"""
    dt = _utc(year=2026, month=10, day=12, hour=15, minute=0)
    assert is_allowed_window(
        dt, None, night_start="22:00", night_end="07:00"
    ) is False


def test_allowed_midnight_crossing_workday_02h() -> None:
    """工作日凌晨 02:00（跨午夜夜间窗口内）→ 允许。"""
    # 北京 2026-10-13 周二 02:00 = UTC 2026-10-12 18:00（前一日）。
    dt = _utc(year=2026, month=10, day=12, hour=18, minute=0)
    assert is_allowed_window(
        dt, _DEFAULT_RULE, night_start="22:00", night_end="07:00"
    ) is True


# --------------------------------------------------------- NightDevPlanner.plan
def test_plan_creates_and_queues_in_night_window(tmp_path: Path) -> None:
    """授权夜间窗口内 → 为每个 spec 创建+入队任务，幂等键正确、状态 QUEUED。"""
    conn = _conn(tmp_path)
    try:
        _active_rule(conn)
        planner = NightDevPlanner(conn, specs=_SPECS)
        now = _utc(year=2026, month=10, day=12, hour=15, minute=0)  # 北京 23:00 周一
        planned = planner.plan(now=now)

        assert len(planned) == 2
        assert [p.created for p in planned] == [True, True]
        queued = _queued_ids(conn)
        assert {p.task_id for p in planned} <= queued
        for p in planned:
            row = _task_row(conn, p.task_id)
            assert row["status"] == "QUEUED"
            assert row["idempotency_key"] == f"nightdev:{p.name}:2026-10-12"
            assert row["task_type"] in {s.task_type for s in _SPECS}
            assert row["max_execution_seconds"] == 3600
    finally:
        conn.close()


def test_plan_no_tasks_workday_daytime(tmp_path: Path) -> None:
    """工作日白天（非授权）→ 返回空，不创建任何任务。"""
    conn = _conn(tmp_path)
    try:
        _active_rule(conn)
        planner = NightDevPlanner(conn, specs=_SPECS)
        now = _utc(year=2026, month=10, day=12, hour=5, minute=0)  # 北京 13:00 周一
        assert planner.plan(now=now) == []
        assert _queued_ids(conn) == set()
    finally:
        conn.close()


def test_plan_creates_on_weekend(tmp_path: Path) -> None:
    """周末本地时间 → 创建+入队。"""
    conn = _conn(tmp_path)
    try:
        _active_rule(conn)
        planner = NightDevPlanner(conn, specs=_SPECS)
        now = _utc(year=2026, month=10, day=17, hour=2, minute=0)  # 北京周六 10:00
        planned = planner.plan(now=now)
        assert len(planned) == 2
        assert all(p.created for p in planned)
        assert len(_queued_ids(conn)) == 2
    finally:
        conn.close()


def test_plan_creates_on_holiday(tmp_path: Path) -> None:
    """传入 holidays 的日期 → 创建+入队。"""
    conn = _conn(tmp_path)
    try:
        _active_rule(conn)
        planner = NightDevPlanner(
            conn, specs=_SPECS, holidays=("2026-10-12",)
        )
        now = _utc(year=2026, month=10, day=12, hour=2, minute=0)  # 北京 10:00 周一(假日)
        planned = planner.plan(now=now)
        assert len(planned) == 2
        assert all(p.created for p in planned)
        assert len(_queued_ids(conn)) == 2
    finally:
        conn.close()


def test_plan_no_tasks_when_rule_missing(tmp_path: Path) -> None:
    """无活跃规则 → 保守不创建任务（未授权时间不启动）。"""
    conn = _conn(tmp_path)
    try:
        # 不写入任何 window_rule。
        planner = NightDevPlanner(conn, specs=_SPECS)
        now = _utc(year=2026, month=10, day=12, hour=15, minute=0)  # 北京 23:00 周一
        assert planner.plan(now=now) == []
        assert _queued_ids(conn) == set()
        assert conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"] == 0
    finally:
        conn.close()


def test_plan_idempotent_within_same_window(tmp_path: Path) -> None:
    """同一授权窗口内调用两次 plan → 第二次去重，created=False 且 task_id 不变。"""
    conn = _conn(tmp_path)
    try:
        _active_rule(conn)
        planner = NightDevPlanner(conn, specs=_SPECS)
        now = _utc(year=2026, month=10, day=12, hour=15, minute=0)  # 北京 23:00 周一

        first = planner.plan(now=now)
        assert all(p.created for p in first)
        first_task_ids = {p.task_id for p in first}

        second = planner.plan(now=now)
        assert len(second) == 2
        assert all(not p.created for p in second)
        assert {p.task_id for p in second} == first_task_ids

        # 每 spec 仅一条任务（按幂等键去重）。
        assert _queued_ids(conn) == first_task_ids
        for key in (
            "nightdev:refactor-core:2026-10-12",
            "nightdev:bump-deps:2026-10-12",
        ):
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM tasks WHERE idempotency_key = ?", (key,)
            ).fetchone()["c"]
            assert count == 1
    finally:
        conn.close()


def test_plan_deterministic_clock_via_now_fn(tmp_path: Path) -> None:
    """注入 now_fn 提供确定性时钟（不依赖真实系统时间）。"""
    conn = _conn(tmp_path)
    try:
        _active_rule(conn)
        fixed = _utc(year=2026, month=10, day=12, hour=15, minute=0)
        planner = NightDevPlanner(conn, specs=_SPECS, now_fn=lambda: fixed)
        planned = planner.plan()
        assert len(planned) == 2
        assert all(p.created for p in planned)
    finally:
        conn.close()
