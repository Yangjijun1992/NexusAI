"""预算硬上限简化版测试（S2-07）。"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

from nexusai_lite import db
from nexusai_lite.budget import (
    BudgetCheck,
    BudgetExceededError,
    BudgetGuard,
)
from nexusai_lite.config import default_config

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """返回已初始化的临时 SQLite 连接（自动应用 005_budget 迁移）。"""
    db_path_ = tmp_path / "nexusai.db"
    db.init_db(db_path_)
    c = db.connect(db_path_)
    yield c
    c.close()


def _audit_types(conn: sqlite3.Connection) -> list[str]:
    """按插入顺序返回审计事件类型列表。"""
    rows = conn.execute("SELECT event_type FROM audit_events ORDER BY id").fetchall()
    return [r["event_type"] for r in rows]


def _insert_cost(
    conn: sqlite3.Connection,
    *,
    provider: str,
    model: str,
    amount: float,
    cost_status: str = "actual",
) -> None:
    """插入一条 cost_records（可求和状态，计入 committed）。"""
    conn.execute(
        "INSERT INTO cost_records "
        "(request_id, provider, model, estimated_cost, actual_cost, cost_status, "
        " effective_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            str(uuid.uuid4()),
            provider,
            model,
            amount,
            amount,
            cost_status,
            db.utcnow_iso(),
            db.utcnow_iso(),
        ),
    )


def test_reserve_rejects_joint_overspend(conn: sqlite3.Connection) -> None:
    """并发竞争防护：两次合计超限的 reserve 中第二次被拒绝（committed=0 情形）。"""
    guard = BudgetGuard(conn)
    guard.set_limits("platform", hard_limit=1.0)
    first = guard.reserve("platform", 0.6)
    assert first.status == "active"
    with pytest.raises(BudgetExceededError):
        guard.reserve("platform", 0.6)
    # 第一次预留仍有效，未因第二次失败回滚。
    assert guard.usage("platform")["reserved"] == 0.6


def test_commit_moves_reserved_to_committed(conn: sqlite3.Connection) -> None:
    """commit 把预留从 reserved 移出，且对应 cost_record 记为 committed。"""
    guard = BudgetGuard(conn)
    guard.set_limits("platform", hard_limit=5.0)
    res = guard.reserve("platform", 0.6)
    assert guard.usage("platform")["reserved"] == 0.6
    assert guard.usage("platform")["committed"] == 0.0
    guard.commit(res.reservation_id, 0.6)
    _insert_cost(conn, provider="deepseek", model="deepseek-chat", amount=0.6)
    u = guard.usage("platform")
    assert u["reserved"] == 0.0
    assert u["committed"] == 0.6
    assert u["available"] == pytest.approx(5.0 - 0.6)


def test_release_frees_capacity(conn: sqlite3.Connection) -> None:
    """release 归还容量：释放后可再次预留。"""
    guard = BudgetGuard(conn)
    guard.set_limits("platform", hard_limit=1.0)
    res = guard.reserve("platform", 0.6)
    guard.release(res.reservation_id)
    assert guard.usage("platform")["reserved"] == 0.0
    guard.reserve("platform", 0.6)  # 不应抛 BudgetExceededError


def test_expire_stale_frees_capacity(conn: sqlite3.Connection) -> None:
    """过期（stale）预留被 expire_stale() 标记 expired 并归还容量。"""
    guard = BudgetGuard(conn)
    guard.set_limits("platform", hard_limit=1.0)
    res = guard.reserve("platform", 0.6, ttl_seconds=100)
    conn.execute(
        "UPDATE budget_reservations SET expires_at=? WHERE reservation_id=?",
        ("2000-01-01T00:00:00+00:00", res.reservation_id),
    )
    assert guard.expire_stale() == 1
    assert guard.usage("platform")["reserved"] == 0.0
    guard.reserve("platform", 0.6)  # 容量已归还，不再超限


def test_check_without_limit_allows_with_limit_set_false(conn: sqlite3.Connection) -> None:
    """未设置硬上限时 check 放行、limit_set=False、would_exceed=False。"""
    guard = BudgetGuard(conn)
    chk = guard.check("platform", 100.0)
    assert isinstance(chk, BudgetCheck)
    assert chk.allowed is True
    assert chk.limit_set is False
    assert chk.would_exceed is False
    assert chk.remaining is None


def test_soft_threshold_crossing_writes_audit(conn: sqlite3.Connection) -> None:
    """越过软阈值时写一条 BudgetThresholdReached 审计。"""
    guard = BudgetGuard(conn)
    guard.set_limits("platform", soft_limit=0.8, hard_limit=2.0)
    guard.reserve("platform", 0.6)
    assert "BudgetThresholdReached" not in _audit_types(conn)
    guard.reserve("platform", 0.6)  # 合计 1.2 > 0.8，越过软阈值
    assert "BudgetThresholdReached" in _audit_types(conn)


def test_per_task_scope_isolation(conn: sqlite3.Connection) -> None:
    """不同 task scope 相互隔离，互不影响上限。"""
    guard = BudgetGuard(conn)
    guard.set_limits("task:a", hard_limit=1.0)
    guard.set_limits("task:b", hard_limit=1.0)
    guard.reserve("task:a", 0.6)
    with pytest.raises(BudgetExceededError):
        guard.reserve("task:a", 0.6)
    guard.reserve("task:b", 0.6)  # 不受 task:a 影响


def test_usage_numbers(conn: sqlite3.Connection) -> None:
    """usage 返回 committed/reserved/available/hard_limit 正确数值。"""
    guard = BudgetGuard(conn)
    guard.set_limits("platform", hard_limit=3.0)
    guard.reserve("platform", 1.0)
    u = guard.usage("platform")
    assert u["committed"] == 0.0
    assert u["reserved"] == 1.0
    assert u["available"] == 2.0
    assert u["hard_limit"] == 3.0


def test_usage_committed_from_cost_records(conn: sqlite3.Connection) -> None:
    """provider scope 的 committed 来自 cost_records；无硬上限时 available 为 None。"""
    guard = BudgetGuard(conn)
    _insert_cost(conn, provider="deepseek", model="deepseek-chat", amount=2.0)
    u = guard.usage("provider:deepseek")
    assert u["committed"] == 2.0
    assert u["available"] is None
    assert u["hard_limit"] is None


def test_set_limits_audit_and_check_reflects_change(conn: sqlite3.Connection) -> None:
    """set_limits 写审计，且限额变化反映到 check。"""
    guard = BudgetGuard(conn)
    guard.set_limits("platform", hard_limit=1.0, soft_limit=0.5, currency="CNY")
    assert "BudgetLimitsChanged" in _audit_types(conn)
    assert guard.check("platform", 0.6).limit_set is True
    assert guard.check("platform", 0.6).would_exceed is False
    assert guard.check("platform", 1.1).would_exceed is True
    # 更新上限后（set_limits 语义为「设置显式传入字段」，显式保留 soft），更高金额可放行。
    guard.set_limits("platform", hard_limit=2.0, soft_limit=0.5)
    assert guard.check("platform", 1.5).would_exceed is False
    assert guard.check("platform", 0.6).soft_exceeded is True  # 越过 soft 0.5


def test_from_config_seeds_platform_only_when_limits_set(conn: sqlite3.Connection) -> None:
    """from_config 只在存在限额时创建 platform 账户；None 限额不臆造。"""
    guard = BudgetGuard(conn)
    cfg = default_config()
    guard.from_config(cfg)  # 默认 budget 全 None → 不创建账户
    assert guard.usage("platform")["hard_limit"] is None
