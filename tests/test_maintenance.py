"""维护入口测试（§3 崩溃恢复 + 过期清理 + 陈旧预留清理）。

覆盖 :func:`nexusai_lite.maintenance.run_maintenance` 的固定顺序维护流程：
从配置播种 ``platform`` 预算上限、清理已过 TTL 的陈旧预留、恢复超期 ``RUNNING``
任务（转 ``UNKNOWN``）、把超截止时间的非终态任务转 ``EXPIRED``。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from nexusai_lite import db
from nexusai_lite.budget import BudgetGuard
from nexusai_lite.config import activate_config, default_config, propose_config
from nexusai_lite.maintenance import run_maintenance
from nexusai_lite.tasks import TaskService, TaskStatus

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """返回已初始化的临时 SQLite 连接。"""
    db_path = tmp_path / "nexusai.db"
    db.init_db(db_path)
    c = db.connect(db_path)
    yield c
    c.close()


def test_run_maintenance_seeds_recover_expire_and_clean(
    conn: sqlite3.Connection,
) -> None:
    """维护：播种 platform 上限、过期陈旧预留、恢复 RUNNING、过期非终态任务。"""
    # 播种配置日上限。
    payload = default_config().model_dump()
    payload["budget"] = {"daily_hard_limit": 100.0}
    version = propose_config(conn, payload, "设置日上限", human_approval=True)
    activate_config(conn, version)

    service = TaskService(conn)

    # 陈旧预留：TTL 已过期。
    stale = BudgetGuard(conn).reserve("platform", 5.0, ttl_seconds=1)
    conn.execute(
        "UPDATE budget_reservations SET expires_at=? WHERE reservation_id=?",
        ("2000-01-01T00:00:00+00:00", stale.reservation_id),
    )

    # 超期 RUNNING 任务 → recover 转 UNKNOWN。
    running = service.create_task(task_type="llm", max_execution_seconds=30)
    service.queue(running.task_id)
    service.start(running.task_id)
    conn.execute(
        "UPDATE tasks SET started_at=? WHERE task_id=?",
        ("2000-01-01T00:00:00+00:00", running.task_id),
    )

    # 超截止时间的 QUEUED 任务 → 非终态超期，转 EXPIRED。
    overdue = service.create_task(
        task_type="llm", deadline_at="2000-01-01T00:00:00+00:00"
    )
    service.queue(overdue.task_id)

    report = run_maintenance(conn)

    # 从配置播种的 platform 上限已生效。
    usage = BudgetGuard(conn).usage("platform")
    assert usage["hard_limit"] == 100.0

    # 陈旧预留被过期（归还容量）。
    assert report.stale_reservations == 1
    row = conn.execute(
        "SELECT status FROM budget_reservations WHERE reservation_id=?",
        (stale.reservation_id,),
    ).fetchone()
    assert row is not None
    assert row["status"] == "expired"

    # RUNNING → UNKNOWN（不盲目重试）。
    running_after = service.get_task(running.task_id)
    assert running_after is not None
    assert running_after.status is TaskStatus.UNKNOWN
    # 非终态超期 → EXPIRED。
    overdue_after = service.get_task(overdue.task_id)
    assert overdue_after is not None
    assert overdue_after.status is TaskStatus.EXPIRED

    # recover() 同时覆盖了 RUNNING 与超期队列任务，expire_overdue() 后继无更多。
    assert report.recovered == 2
    assert report.expired == 0


def test_run_maintenance_is_idempotent(conn: sqlite3.Connection) -> None:
    """维护可安全重复调用，重复执行不改变已终态任务。"""
    service = TaskService(conn)
    pending = service.create_task(task_type="llm")
    service.queue(pending.task_id)

    first = run_maintenance(conn)
    second = run_maintenance(conn)

    assert first.recovered == 0
    assert first.expired == 0
    assert first.stale_reservations == 0
    assert second.recovered == 0
    # 已成功入队的任务无截止时间且不超期，保持不变。
    pending_after = service.get_task(pending.task_id)
    assert pending_after is not None
    assert pending_after.status is TaskStatus.QUEUED
