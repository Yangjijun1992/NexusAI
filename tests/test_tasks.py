"""S2-01 / S2-02 / S2-03 测试：任务状态机、幂等创建、有限重试与恢复。

覆盖验收标准：状态转换合法性、幂等键去重（含 UNIQUE 竞态路径）、有限重试
（次数 / 成本上限）、保守恢复（RUNNING 超期 → UNKNOWN、超期 → EXPIRED）、
``UNKNOWN`` 人工处置、取消记录与终态不可变、状态过滤。均使用临时 SQLite 库。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from nexusai_lite import db
from nexusai_lite.tasks import (
    InvalidTransitionError,
    Task,
    TaskService,
    TaskStatus,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path


def _past_iso(days: int = 1) -> str:
    """返回 ``days`` 天前的 UTC ISO 时间戳。"""
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def _future_iso(days: int = 1) -> str:
    """返回 ``days`` 天后的 UTC ISO 时间戳。"""
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


@pytest.fixture
def service(tmp_path: Path) -> Iterator[TaskService]:
    """初始化临时库并返回 TaskService（连接由测试负责关闭）。"""
    db_path = tmp_path / "tasks.db"
    db.init_db(db_path)
    conn = db.connect(db_path)
    yield TaskService(conn)
    conn.close()


def _run_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) AS c FROM task_runs").fetchone()["c"]


def _attempt_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) AS c FROM task_attempts").fetchone()["c"]


# ------------------------------------------------------------------ 状态机 happy path


def test_happy_path_with_run_and_attempt_rows(service: TaskService) -> None:
    """PENDING→QUEUED→RUNNING→SUCCEEDED，各时间戳落库，run/attempt 行写入。"""
    task = service.create_task(task_type="llm")
    assert task.status is TaskStatus.PENDING
    assert task.attempt_no == 0
    assert task.created_at is not None
    assert task.created_at
    assert task.started_at is None
    assert task.finished_at is None

    service.queue(task.task_id)
    service.start_run(task.task_id)

    queued = service.get_task(task.task_id)
    assert queued is not None
    assert queued.status is TaskStatus.RUNNING
    assert queued.started_at is not None

    finished = service.succeed(queued.task_id, actual_cost=0.01, cost_status="actual")
    assert finished.status is TaskStatus.SUCCEEDED
    assert finished.finished_at is not None
    assert finished.actual_cost == pytest.approx(0.01)
    assert finished.cost_status == "actual"
    assert _run_count(service.conn) == 1
    assert _attempt_count(service.conn) == 0


def test_start_run_writes_run_row_and_delivers_execution_id(
    service: TaskService, tmp_path: Path
) -> None:
    """start_run 返回 execution_id 并写入 task_runs 行；record_attempt 落库。"""
    task = service.create_task(task_type="llm")
    service.queue(task.task_id)
    execution_id = service.start_run(task.task_id)
    assert execution_id

    attempt_id = service.record_attempt(
        execution_id=execution_id,
        task_id=task.task_id,
        attempt_no=1,
        started_at=task.created_at,
        retry_reason="rate_limited",
        may_be_billed=True,
        cost_estimate=0.001,
    )
    assert attempt_id

    row = service.conn.execute(
        "SELECT execution_id, task_id FROM task_runs WHERE execution_id = ?", (execution_id,)
    ).fetchone()
    assert row is not None
    assert row["task_id"] == task.task_id

    attempt = service.conn.execute(
        "SELECT attempt_no, retry_reason, may_be_billed FROM task_attempts "
        "WHERE attempt_id = ?",
        (attempt_id,),
    ).fetchone()
    assert attempt is not None
    assert attempt["attempt_no"] == 1
    assert attempt["retry_reason"] == "rate_limited"
    assert attempt["may_be_billed"] == 1


def test_finish_run_closes_row(service: TaskService) -> None:
    """finish_run 写入 finished_at / status / error 字段。"""
    task = service.create_task(task_type="llm")
    service.queue(task.task_id)
    execution_id = service.start_run(task.task_id)
    service.finish_run(
        execution_id, status="failed", error_code="E1", safe_error_message="boom"
    )
    row = service.conn.execute(
        "SELECT status, error_code, safe_error_message, finished_at FROM task_runs "
        "WHERE execution_id = ?",
        (execution_id,),
    ).fetchone()
    assert row is not None
    assert row["status"] == "failed"
    assert row["error_code"] == "E1"
    assert row["safe_error_message"] == "boom"
    assert row["finished_at"] is not None


# ------------------------------------------------------------------ 非法转换


def test_illegal_transition_success_is_terminal(service: TaskService) -> None:
    """SUCCEEDED 为终态：无法再转 RUNNING。"""
    task = service.create_task(task_type="llm")
    service.queue(task.task_id)
    service.start_run(task.task_id)
    service.succeed(task.task_id)
    with pytest.raises(InvalidTransitionError):
        service.start(task.task_id)


def test_illegal_transition_pending_to_succeeded(service: TaskService) -> None:
    """PENDING 不能直接转 SUCCEEDED（需经 QUEUED→RUNNING）。"""
    task = service.create_task(task_type="llm")
    with pytest.raises(InvalidTransitionError):
        service.succeed(task.task_id)


def test_illegal_transition_headless_retry(service: TaskService) -> None:
    """PENDING 不能直接进入 RETRY_WAIT。"""
    task = service.create_task(task_type="llm")
    with pytest.raises(InvalidTransitionError):
        service.schedule_retry(task.task_id, "why")


# ------------------------------------------------------------------ 幂等创建与去重


def test_idempotent_create_same_key_same_task(service: TaskService) -> None:
    """同一幂等键重复提交返回同一 task_id，且仅 1 行。"""
    first = service.create_task(task_type="llm", idempotency_key="msg-42")
    second = service.create_task(task_type="llm", idempotency_key="msg-42")
    assert first.task_id == second.task_id
    row = service.conn.execute(
        "SELECT COUNT(*) AS c FROM tasks WHERE idempotency_key = 'msg-42'"
    ).fetchone()
    assert row is not None
    assert row["c"] == 1


def test_idempotent_create_does_not_spawn_second_run(service: TaskService) -> None:
    """重复投递（相同幂等键）不重复执行：同一任务仅 1 个 run。"""
    first = service.create_task(task_type="llm", idempotency_key="delivery-1")
    service.queue(first.task_id)
    service.start_run(first.task_id)

    duplicate = service.create_task(task_type="llm", idempotency_key="delivery-1")
    assert duplicate.task_id == first.task_id
    assert _run_count(service.conn) == 1


def test_create_race_via_unique_constraint(
    service: TaskService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """并发竞态：预检扑空后 INSERT 撞 UNIQUE，回退路径返回既有任务。"""
    original = service.create_task(task_type="llm", idempotency_key="race-1")
    real = TaskService._find_by_key
    state = {"skip": 1}

    def flaky(self: TaskService, key: str) -> Task | None:
        if state["skip"] > 0:
            state["skip"] -= 1
            return None
        return real(self, key)

    monkeypatch.setattr(TaskService, "_find_by_key", flaky)
    got = service.create_task(task_type="llm", idempotency_key="race-1")
    assert got.task_id == original.task_id
    assert service.conn.execute(
        "SELECT COUNT(*) AS c FROM tasks WHERE idempotency_key = 'race-1'"
    ).fetchone()["c"] == 1


# ------------------------------------------------------------------ 有限重试


def test_bounded_retry_increments_attempt_and_fails_when_exceeded(
    service: TaskService,
) -> None:
    """max_attempts=3（含首次执行的总次数）：允许 2 次重试，再重试转 FAILED。"""
    task = service.create_task(task_type="llm", max_attempts=3)
    service.queue(task.task_id)
    service.start_run(task.task_id)

    task = service.schedule_retry(task.task_id, "rate_limited")
    assert task.status is TaskStatus.RETRY_WAIT
    assert task.attempt_no == 1

    service.queue(task.task_id)
    service.start_run(task.task_id)
    task = service.schedule_retry(task.task_id, "overloaded")
    assert task.status is TaskStatus.RETRY_WAIT
    assert task.attempt_no == 2

    # 第 3 次执行（含首次）后已达 max_attempts=3 → FAILED（不再进入 RETRY_WAIT）
    service.queue(task.task_id)
    service.start_run(task.task_id)
    task = service.schedule_retry(task.task_id, "again")
    assert task.status is TaskStatus.FAILED
    assert task.error_code == "MAX_ATTEMPTS_OR_BUDGET"
    assert task.finished_at is not None


def test_budget_breach_blocks_retry(service: TaskService) -> None:
    """estimated_cost 突破 max_cost → schedule_retry 转 FAILED（成本上限）。"""
    task = service.create_task(
        task_type="llm", max_attempts=5, max_cost=1.0, estimated_cost=2.5
    )
    service.queue(task.task_id)
    service.start_run(task.task_id)
    task = service.schedule_retry(task.task_id, "budget")
    assert task.status is TaskStatus.FAILED
    assert task.error_code == "MAX_ATTEMPTS_OR_BUDGET"


# ------------------------------------------------------------------ 恢复


def test_recover_running_stale_goes_unknown_without_new_run(
    service: TaskService,
) -> None:
    """RUNNING 超过执行窗口 → UNKNOWN，且不创建新的 run/attempt（不盲目重试）。"""
    task = service.create_task(task_type="llm", max_execution_seconds=60)
    service.queue(task.task_id)
    service.start_run(task.task_id)
    # 人为把 started_at 拨回 1 天前，模拟执行进程崩溃后留下的陈旧运行。
    service.conn.execute(
        "UPDATE tasks SET started_at = ? WHERE task_id = ?",
        (_past_iso(), task.task_id),
    )
    affected = service.recover()
    assert affected == [task.task_id]
    got = service.get_task(task.task_id)
    assert got is not None
    assert got.status is TaskStatus.UNKNOWN
    assert _run_count(service.conn) == 1
    assert _attempt_count(service.conn) == 0


def test_recover_overdue_task_expires(service: TaskService) -> None:
    """超过 deadline_at 的非终态任务 → EXPIRED。"""
    task = service.create_task(task_type="llm", deadline_at=_past_iso())
    service.queue(task.task_id)
    affected = service.recover()
    assert task.task_id in affected
    got = service.get_task(task.task_id)
    assert got is not None
    assert got.status is TaskStatus.EXPIRED


def test_expire_overdue(service: TaskService) -> None:
    """expire_overdue 把超期非终态任务转 EXPIRED，不影响未超期任务。"""
    overdue = service.create_task(task_type="llm", deadline_at=_past_iso())
    service.queue(overdue.task_id)
    fresh = service.create_task(task_type="llm", deadline_at=_future_iso())
    service.queue(fresh.task_id)
    affected = service.expire_overdue()
    assert affected == [overdue.task_id]
    overdue_got = service.get_task(overdue.task_id)
    assert overdue_got is not None
    assert overdue_got.status is TaskStatus.EXPIRED
    fresh_got = service.get_task(fresh.task_id)
    assert fresh_got is not None
    assert fresh_got.status is TaskStatus.QUEUED


# ------------------------------------------------------------------ UNKNOWN 人工处置


def test_mark_unknown_and_resolve_to_queued(service: TaskService) -> None:
    """RUNNING → UNKNOWN（mark_unknown），再 resolve 回 QUEUED。"""
    task = service.create_task(task_type="llm")
    service.queue(task.task_id)
    service.start_run(task.task_id)
    service.mark_unknown(task.task_id, "timeout, side effects unknown")
    unknown = service.get_task(task.task_id)
    assert unknown is not None
    assert unknown.status is TaskStatus.UNKNOWN
    assert "timeout" in (unknown.safe_error_message or "")

    resolved = service.resolve_unknown(task.task_id, TaskStatus.QUEUED)
    assert resolved.status is TaskStatus.QUEUED


def test_resolve_unknown_only_three_outcomes(service: TaskService) -> None:
    """UNKNOWN 仅可转 QUEUED / FAILED / SUCCEEDED；其余目标非法。"""
    task = service.create_task(task_type="llm")
    service.queue(task.task_id)
    service.start_run(task.task_id)
    service.mark_unknown(task.task_id, "uncertain")
    # 合法目标
    service.resolve_unknown(task.task_id, TaskStatus.FAILED)
    resolved = service.get_task(task.task_id)
    assert resolved is not None
    assert resolved.status is TaskStatus.FAILED

    # 非法目标：从 FAILED（终态）无法再转 RUNNING
    with pytest.raises(InvalidTransitionError):
        service.resolve_unknown(task.task_id, TaskStatus.RUNNING)


def test_mark_unknown_only_from_running(service: TaskService) -> None:
    """mark_unknown 只允许从 RUNNING（非 RUNNING 任务不得进入 UNKNOWN）。"""
    task = service.create_task(task_type="llm")
    with pytest.raises(InvalidTransitionError):
        service.mark_unknown(task.task_id, "bad")


# ------------------------------------------------------------------ 取消 / 暂停 / 终态不可变


def test_cancel_records_reason_and_time(service: TaskService) -> None:
    """取消记录时间与残余外部执行提示。"""
    task = service.create_task(task_type="llm")
    service.queue(task.task_id)
    cancelled = service.cancel(task.task_id, "manual abort")
    assert cancelled.status is TaskStatus.CANCELLED
    assert cancelled.finished_at is not None
    assert cancelled.error_code == "CANCELLED"
    assert "外部副作用可能已发生" in (cancelled.safe_error_message or "")


def test_terminal_state_immutable(service: TaskService) -> None:
    """CANCELLED 为终态：无法再 queue / start / succeed。"""
    task = service.create_task(task_type="llm")
    service.queue(task.task_id)
    service.cancel(task.task_id, "stop")
    with pytest.raises(InvalidTransitionError):
        service.queue(task.task_id)
    with pytest.raises(InvalidTransitionError):
        service.start(task.task_id)
    with pytest.raises(InvalidTransitionError):
        service.succeed(task.task_id)


def test_pause_and_unpause(service: TaskService) -> None:
    """QUEUED → PAUSED → QUEUED。"""
    task = service.create_task(task_type="llm")
    service.queue(task.task_id)
    paused = service.pause(task.task_id)
    assert paused.status is TaskStatus.PAUSED
    resumed = service.unpause(task.task_id)
    assert resumed.status is TaskStatus.QUEUED


# ------------------------------------------------------------------ 列表过滤


def test_list_tasks_filters_by_status(service: TaskService) -> None:
    """list_tasks 可按状态过滤。"""
    a = service.create_task(task_type="llm")
    b = service.create_task(task_type="dev")
    service.queue(a.task_id)
    service.queue(b.task_id)
    service.start_run(a.task_id)

    running = service.list_tasks(status=TaskStatus.RUNNING)
    assert [t.task_id for t in running] == [a.task_id]

    queued = service.list_tasks(status="QUEUED")
    assert [t.task_id for t in queued] == [b.task_id]

    all_tasks = service.list_tasks(limit=100)
    assert {t.task_id for t in all_tasks} == {a.task_id, b.task_id}
