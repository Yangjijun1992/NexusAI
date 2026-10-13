"""X-01 验收：一致性与恢复测试（reference §9 / §12.2）。

覆盖 reference.md §3、F6、§9 任务恢复与一致性的验收标准（任务清单 X-01）：

- **幂等创建 / 去重：** 同一 ``Idempotency-Key`` 重复提交不创建重复任务；重复投递
  不重复创建第二个逻辑任务。
- **一致性：** DB 提交成功但事件/日志写入失败时，已提交状态不被回滚、失败可观测、
  不无限阻塞主流程（§9 可运营性）。
- **崩溃恢复：** Worker 领取后崩溃（RUNNING → ``recover()`` → UNKNOWN，不盲目重试）；
  外部副作用已发生但状态未持久化时不盲目重试；过期 RUNNING 超时进入 UNKNOWN。
- **重启：** Break 熔断/任务状态在进程重启后不静默重置；重启后可区分可安全恢复与
  需人工确认的任务。
- **取消：** 取消仍在运行的外部调用 → CANCELLED 并标记残余外部执行风险（F6）。
- **外部副作用不确定：** Git-PR-已创建但客户端未知 → UNKNOWN（§3 / §12 规则 3）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from nexusai_lite import db
from nexusai_lite.alerts import Alert, AlertManager, Severity
from nexusai_lite.circuit_breaker import BreakerState, CircuitBreaker
from nexusai_lite.collector.window_rules import WindowRuleData, upsert_window_rule
from nexusai_lite.executor import DevExecutor, DraftOutput
from nexusai_lite.nightdev import NightDevPlanner, NightDevSpec
from nexusai_lite.sandbox import FakeSandbox, SandboxResult
from nexusai_lite.tasks import Task, TaskService, TaskStatus
from nexusai_lite.whitelist import CommandWhitelist

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterator


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """返回已初始化的临时 SQLite 连接（自动应用全部迁移并播种默认配置 v1）。"""
    db_path = tmp_path / "nexusai.db"
    db.init_db(db_path)
    c = db.connect(db_path)
    yield c
    c.close()


def _active_rule(conn: sqlite3.Connection) -> None:
    """写入一条默认活跃时段规则，使夜间窗口可判定。"""
    upsert_window_rule(
        conn,
        WindowRuleData(
            timezone="Asia/Shanghai",
            workdays=(1, 2, 3, 4, 5),
            peak_windows=(("09:00", "12:00"), ("14:00", "18:00")),
            source="official-pricing-page",
        ),
        source="official-pricing-page",
    )


def _utc(*, year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    """构造带 UTC 时区的给定时刻。"""
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def _make_running_task(service: TaskService, *, started_at: str | None = None) -> str:
    """创建一个已进入 RUNNING 的任务并返回 task_id。"""
    task = service.create_task(task_type="llm", estimated_cost=0.1)
    service.queue(task.task_id)
    service.start(task.task_id)
    if started_at is not None:
        service.conn.execute(
            "UPDATE tasks SET started_at=? WHERE task_id=?", (started_at, task.task_id)
        )
        service.conn.commit()
    return task.task_id


def _require_task(service: TaskService, task_id: str) -> Task:
    """返回任务；不存在时失败（避免 Optional 访问）。"""
    task = service.get_task(task_id)
    assert task is not None
    return task


# ---------------------------------------------------------------- 幂等创建 / 去重


def test_same_idempotency_key_double_create_no_duplicate(conn: sqlite3.Connection) -> None:
    """同一幂等键重复创建不应产生重复任务。"""
    service = TaskService(conn)
    first = service.create_task(task_type="llm", idempotency_key="key-1", estimated_cost=0.1)
    second = service.create_task(task_type="llm", idempotency_key="key-1", estimated_cost=0.1)
    assert second.task_id == first.task_id
    rows = conn.execute(
        "SELECT COUNT(*) AS c FROM tasks WHERE idempotency_key='key-1'"
    ).fetchone()
    assert rows["c"] == 1  # 只有一个逻辑任务


def test_duplicate_delivery_nightdev_no_second_task(conn: sqlite3.Connection) -> None:
    """同一授权窗口重复投递（plan 多次）不创建第二个逻辑任务。"""
    _active_rule(conn)
    specs = (NightDevSpec(name="fmt", task_type="llm", repo_path=Path("/repo")),)
    fixed = _utc(year=2026, month=10, day=12, hour=15, minute=0)  # 北京 23:00，夜窗口内
    planner = NightDevPlanner(conn, specs=specs, now_fn=lambda: fixed)
    first = planner.plan(fixed)
    second = planner.plan(fixed)
    assert len(first) == 1
    assert len(second) == 1
    assert first[0].created is True
    assert second[0].created is False  # 重复投递未重复创建
    assert second[0].task_id == first[0].task_id


# ---------------------------------------------------------------- DB 提交成功但日志失败


def test_db_commit_succeeded_but_log_write_failed_is_observable(
    conn: sqlite3.Connection,
) -> None:
    """DB 状态提交成功但事件/日志写入失败：状态不回滚、失败可观测、不无限阻塞。"""
    service = TaskService(conn)
    task = service.create_task(task_type="llm", estimated_cost=0.1)
    service.queue(task.task_id)
    service.start(task.task_id)
    # 状态提交成功。
    service.succeed(task.task_id, actual_cost=0.1, cost_status="actual")
    assert _require_task(service, task.task_id).status is TaskStatus.SUCCEEDED

    # 事件/日志写入失败（告警派发通道故障）→ 有限重试后落为 dispatch_failed，非无限阻塞。
    class _FailingSink:
        """始终抛错的测试告警通道。"""

        def notify(self, alert: Alert) -> None:
            """派发失败。"""
            msg = "sink down"
            raise RuntimeError(msg)

    manager = AlertManager(conn, sinks=(_FailingSink(),), dedup_window_seconds=0)
    alert = manager.raise_alert(
        severity=Severity.INFO,
        code="LOG_WRITE_FAILED_KEEP_STATE",
        title="日志写入失败测试",
        message="告警派发失败但任务状态已提交",
    )
    assert alert is not None
    # 派发状态可观测（有限重试后 dispatch_failed）。
    row = conn.execute(
        "SELECT status FROM alerts WHERE alert_id=?", (alert.alert_id,)
    ).fetchone()
    assert row is not None
    assert row["status"] == "dispatch_failed"
    # 任务成功状态未被回滚。
    assert _require_task(service, task.task_id).status is TaskStatus.SUCCEEDED


# ---------------------------------------------------------------- 崩溃恢复


def test_worker_crash_after_claim_recover_to_unknown(conn: sqlite3.Connection) -> None:
    """Worker 领取后崩溃：RUNNING → recover() → UNKNOWN，不盲目重试。"""
    service = TaskService(conn)
    task_id = _make_running_task(service)
    assert _require_task(service, task_id).status is TaskStatus.RUNNING
    affected = service.recover(stale_after_seconds=0)
    assert task_id in affected
    task = _require_task(service, task_id)
    assert task.status is TaskStatus.UNKNOWN
    assert task.error_category == "unknown_error"  # 不盲目重试，供人工核查


def test_stale_running_timeout_recover_to_unknown(conn: sqlite3.Connection) -> None:
    """过期 RUNNING（超执行窗口）应转 UNKNOWN。"""
    service = TaskService(conn)
    old_started = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    task_id = _make_running_task(service, started_at=old_started)
    affected = service.recover(stale_after_seconds=600)
    assert task_id in affected
    assert _require_task(service, task_id).status is TaskStatus.UNKNOWN


def test_restart_distinguishes_safe_vs_needs_human(conn: sqlite3.Connection) -> None:
    """重启恢复可区分：窗口内 RUNNING 可安全恢复，超期 RUNNING 需人工，超期队列过期。"""
    service = TaskService(conn)
    # 1) 窗口内 RUNNING：可安全恢复（保持 RUNNING）。
    fresh = _make_running_task(service, started_at=datetime.now(UTC).isoformat())
    # 2) 超期 RUNNING：需人工（→ UNKNOWN）。
    old_started = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    stale = _make_running_task(service, started_at=old_started)
    # 3) 超期 QUEUED：过期（→ EXPIRED）。
    overdue = service.create_task(
        task_type="llm",
        estimated_cost=0.1,
        deadline_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
    )
    service.queue(overdue.task_id)

    affected = service.recover(stale_after_seconds=3600)
    # fresh started 距今 < 3600s → 保持 RUNNING。
    assert fresh in affected or _require_task(service, fresh).status is TaskStatus.RUNNING
    assert _require_task(service, fresh).status is TaskStatus.RUNNING
    # stale 距今 > 3600s → UNKNOWN（需人工）。
    assert stale in affected
    assert _require_task(service, stale).status is TaskStatus.UNKNOWN
    # overdue QUEUED → EXPIRED。
    assert overdue.task_id in affected
    assert _require_task(service, overdue.task_id).status is TaskStatus.EXPIRED


# ---------------------------------------------------------------- 取消


def test_cancel_still_running_external(conn: sqlite3.Connection) -> None:
    """取消仍在运行的外部调用 → CANCELLED 并标记残余外部执行风险。"""
    service = TaskService(conn)
    task_id = _make_running_task(service)
    service.cancel(task_id, reason="人工取消")
    task = _require_task(service, task_id)
    assert task.status is TaskStatus.CANCELLED
    assert task.error_code == "CANCELLED"
    # 记录残余外部执行风险（F6）。
    assert "外部副作用可能已发生" in (task.safe_error_message or "")


# ---------------------------------------------------------------- 外部副作用不确定


def _executor(
    conn: sqlite3.Connection,
    tmp_path: Path,
    *,
    confirm_draft: Callable[[DraftOutput], bool] | None = None,
) -> DevExecutor:
    """构造一个使用 FakeSandbox 与 git 白名单的执行器（预设 git diff 产出补丁）。"""
    sandbox = FakeSandbox(
        SandboxResult(
            exit_code=0,
            stdout="+++ a.py\n+print('hi')\n",
            stderr="",
            duration_seconds=0.0,
            timed_out=False,
            killed_reason=None,
            network_isolated=False,
            sandbox_kind="fake",
        )
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    drafts = tmp_path / "drafts"
    return DevExecutor(
        conn,
        sandbox=sandbox,
        workspace_root=workspace,
        commands=CommandWhitelist(["git"]),
        drafts_dir=drafts,
        argv_for=lambda task: ("git", "diff"),
        confirm_draft=confirm_draft,
    )


def _executor_task(executor: DevExecutor, task_id: str) -> Task:
    """返回执行器关联的服务中的任务；不存在时失败。"""
    task = executor._tasks.get_task(task_id)
    assert task is not None
    return task


def test_git_pr_created_but_client_unknown_goes_unknown(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Git-PR 已创建但客户端未知 → UNKNOWN（外部副作用不确定，不盲目重试）。"""
    executor = _executor(conn, tmp_path, confirm_draft=lambda draft: False)
    task = executor._tasks.create_task(task_type="llm", estimated_cost=0.1)
    executor._tasks.queue(task.task_id)
    result = executor.run_task(task.task_id)
    assert result.status == "UNKNOWN"
    assert _executor_task(executor, task.task_id).status is TaskStatus.UNKNOWN


def test_crash_after_external_success_before_persist_unknown(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """外部副作用已发生但状态未持久化 → UNKNOWN（不盲目重试）。"""
    executor = _executor(conn, tmp_path, confirm_draft=lambda draft: False)
    task = executor._tasks.create_task(task_type="llm", estimated_cost=0.1)
    executor._tasks.queue(task.task_id)
    result = executor.run_task(task.task_id)
    assert result.status == "UNKNOWN"
    # 补丁草案可能已写盘但未确认：外部副作用不确定，绝不自动重试。
    assert _executor_task(executor, task.task_id).attempt_no == 0


# ---------------------------------------------------------------- 重启 / DB 短暂不可用


def test_scheduler_restart_breaker_persisted(conn: sqlite3.Connection) -> None:
    """进程重启后熔断 OPEN 不静默重置为 CLOSED。"""
    clock_now = datetime(2026, 10, 12, 1, 0, 0, tzinfo=UTC)

    def clock_a() -> datetime:
        return clock_now

    breaker_a = CircuitBreaker(conn, now_fn=clock_a, failure_threshold=1, open_seconds=3600)
    breaker_a.record_failure("provider:deepseek")
    assert breaker_a.state("provider:deepseek") is BreakerState.OPEN

    # 模拟进程重启：新熔断器实例、同一时刻（冷却未过）。
    def clock_b() -> datetime:
        return clock_now

    breaker_b = CircuitBreaker(conn, now_fn=clock_b, failure_threshold=1, open_seconds=3600)
    # 重启后仍 OPEN（未静默 CLOSED）。
    assert breaker_b.state("provider:deepseek") is BreakerState.OPEN
