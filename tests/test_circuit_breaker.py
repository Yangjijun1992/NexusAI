"""简化熔断 CLOSED/OPEN/HALF_OPEN 测试（S2-05）。

使用临时 SQLite 数据库与可注入时钟，确定性验证状态转换、在途探测限制、冷却转换、
审计与跨连接持久化（reference F5、§4.4）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, TypedDict, Unpack

import pytest

from nexusai_lite import db
from nexusai_lite.circuit_breaker import BreakerState, CircuitBreaker

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path


class _Clock:
    """可推进的可注入时钟（返回 aware UTC datetime）。"""

    def __init__(self, start: datetime | None = None) -> None:
        """初始化时钟到给定起点（默认当前 UTC）。"""
        self._now = start or datetime.now(UTC)
        if self._now.tzinfo is None:
            self._now = self._now.replace(tzinfo=UTC)

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


def _audit_events(conn: sqlite3.Connection) -> list[str]:
    """返回按时间顺序的熔断审计事件类型列表。"""
    rows = conn.execute(
        "SELECT event_type FROM audit_events WHERE object_type='circuit_breakers' "
        "ORDER BY id"
    ).fetchall()
    return [r["event_type"] for r in rows]


class _BreakerKwargs(TypedDict, total=False):
    """熔断器构造参数子集（供测试注入时钟时精确转发）。"""

    failure_threshold: int
    open_seconds: float
    half_open_max_probes: int
    success_threshold: int


def _breaker(
    conn: sqlite3.Connection,
    clock: _Clock,
    **kwargs: Unpack[_BreakerKwargs],
) -> CircuitBreaker:
    """构造一个注入时钟的熔断器。"""
    return CircuitBreaker(conn, now_fn=clock, **kwargs)


def test_initial_state_closed(conn: sqlite3.Connection) -> None:
    """未知维度初始应为 CLOSED。"""
    breaker = _breaker(conn, _Clock())
    assert breaker.state("provider:deepseek") is BreakerState.CLOSED
    assert breaker.allow("provider:deepseek") is True


def test_closed_to_open_at_threshold(conn: sqlite3.Connection) -> None:
    """CLOSED 下连续失败达到 threshold 应打开为 OPEN 并审计。"""
    clock = _Clock()
    breaker = _breaker(conn, clock, failure_threshold=3)
    dim = "provider:deepseek"
    assert breaker.state(dim) is BreakerState.CLOSED
    breaker.record_failure(dim)
    breaker.record_failure(dim)
    assert breaker.state(dim) is BreakerState.CLOSED
    breaker.record_failure(dim)
    assert breaker.state(dim) is BreakerState.OPEN
    assert not breaker.allow(dim)
    assert _audit_events(conn) == ["CircuitBreakerOpened"]


def test_open_blocks_allow(conn: sqlite3.Connection) -> None:
    """OPEN 应拒绝 allow。"""
    clock = _Clock()
    breaker = _breaker(conn, clock, failure_threshold=1)
    dim = "model:deepseek-chat"
    breaker.record_failure(dim)
    assert breaker.state(dim) is BreakerState.OPEN
    assert breaker.allow(dim) is False


def test_open_to_half_open_after_cooldown(conn: sqlite3.Connection) -> None:
    """冷却期过后应自动进入 HALF_OPEN 并审计。"""
    clock = _Clock()
    breaker = _breaker(conn, clock, failure_threshold=1, open_seconds=10.0)
    dim = "provider:deepseek"
    breaker.record_failure(dim)
    assert breaker.state(dim) is BreakerState.OPEN
    # 冷却未到，仍 OPEN。
    clock.advance(5.0)
    assert breaker.state(dim) is BreakerState.OPEN
    # 超过冷却，进入 HALF_OPEN。
    clock.advance(6.0)
    assert breaker.state(dim) is BreakerState.HALF_OPEN
    assert _audit_events(conn) == ["CircuitBreakerOpened", "CircuitBreakerHalfOpened"]


def test_half_open_allows_max_concurrent_probes(conn: sqlite3.Connection) -> None:
    """HALF_OPEN 下只允许 half_open_max_probes 个并发探测。"""
    clock = _Clock()
    breaker = _breaker(
        conn, clock, failure_threshold=1, open_seconds=1.0, half_open_max_probes=2
    )
    dim = "provider:deepseek"
    breaker.record_failure(dim)
    assert breaker.state(dim) is BreakerState.OPEN
    clock.advance(2.0)  # 进入 HALF_OPEN
    # 前两个探测允许，第三个被拒。
    assert breaker.allow(dim) is True
    assert breaker.allow(dim) is True
    assert breaker.allow(dim) is False
    # 释放一个探测后再允许一个。
    breaker.release_probe(dim, success=True)
    assert breaker.allow(dim) is True


def test_half_open_success_threshold_to_closed(conn: sqlite3.Connection) -> None:
    """HALF_OPEN 下连续成功达到 threshold 应恢复 CLOSED 并审计。"""
    clock = _Clock()
    breaker = _breaker(
        conn, clock, failure_threshold=2, open_seconds=1.0, success_threshold=2
    )
    dim = "provider:deepseek"
    breaker.record_failure(dim)
    breaker.record_failure(dim)
    assert breaker.state(dim) is BreakerState.OPEN
    clock.advance(2.0)
    assert breaker.state(dim) is BreakerState.HALF_OPEN
    # 第一次成功：仍 HALF_OPEN。
    breaker.release_probe(dim, success=True)
    assert breaker.state(dim) is BreakerState.HALF_OPEN
    # 第二次成功：恢复 CLOSED。
    breaker.release_probe(dim, success=True)
    assert breaker.state(dim) is BreakerState.CLOSED
    events = _audit_events(conn)
    assert events.count("CircuitBreakerOpened") == 1
    assert events.count("CircuitBreakerHalfOpened") == 1
    assert events.count("CircuitBreakerClosed") == 1
    # 恢复后 allow 正常。
    assert breaker.allow(dim) is True


def test_half_open_probe_failure_opens_again(conn: sqlite3.Connection) -> None:
    """HALF_OPEN 下探测失败应重新打开为 OPEN 并审计（重置 opened_at）。"""
    clock = _Clock()
    breaker = _breaker(conn, clock, failure_threshold=1, open_seconds=1.0)
    dim = "provider:deepseek"
    breaker.record_failure(dim)
    assert breaker.state(dim) is BreakerState.OPEN
    clock.advance(2.0)
    assert breaker.state(dim) is BreakerState.HALF_OPEN
    # 探测失败 → 重新 OPEN。
    breaker.release_probe(dim, success=False)
    assert breaker.state(dim) is BreakerState.OPEN
    events = _audit_events(conn)
    assert events.count("CircuitBreakerOpened") == 2
    assert events.count("CircuitBreakerHalfOpened") == 1


def test_record_success_failure_decrement_inflight(conn: sqlite3.Connection) -> None:
    """record_success/record_failure 应释放 HALF_OPEN 在途探测。"""
    clock = _Clock()
    breaker = _breaker(conn, clock, failure_threshold=1, open_seconds=1.0, half_open_max_probes=1)
    dim = "provider:deepseek"
    breaker.record_failure(dim)
    clock.advance(2.0)
    assert breaker.state(dim) is BreakerState.HALF_OPEN
    assert breaker.allow(dim) is True
    # 在途已满，拒绝新探测。
    assert breaker.allow(dim) is False
    breaker.record_failure(dim)  # 探测失败 → OPEN，释放在途。
    assert breaker.state(dim) is BreakerState.OPEN


def test_persistence_across_connection(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """重启（新连接）后 OPEN 状态应保持（持久化）。"""
    clock = _Clock()
    breaker = _breaker(conn, clock, failure_threshold=2)
    dim = "project:alpha"
    breaker.record_failure(dim)
    breaker.record_failure(dim)
    assert breaker.state(dim) is BreakerState.OPEN
    conn.close()

    # 新连接读取同一 DB 文件。
    db_path_ = tmp_path / "nexusai.db"
    conn2 = db.connect(db_path_)
    try:
        clock2 = _Clock()
        breaker2 = _breaker(conn2, clock2, failure_threshold=2)
        assert breaker2.state(dim) is BreakerState.OPEN
        assert breaker2.allow(dim) is False
        # 冷却后仍能进入 HALF_OPEN（而非直接 CLOSED）。
        clock2.advance(100.0)
        assert breaker2.state(dim) is BreakerState.HALF_OPEN
        assert _audit_events(conn2).count("CircuitBreakerOpened") == 1
    finally:
        conn2.close()


def test_audit_rows_present(conn: sqlite3.Connection) -> None:
    """每次状态转换都应产生审计事件（含 detail_json）。"""
    clock = _Clock()
    breaker = _breaker(conn, clock, failure_threshold=1, open_seconds=1.0)
    dim = "provider:deepseek"
    breaker.record_failure(dim)  # OPEN
    clock.advance(2.0)
    breaker.state(dim)  # HALF_OPEN
    breaker.release_probe(dim, success=True)
    breaker.release_probe(dim, success=True)  # 需 2 次成功才 CLOSED
    rows = conn.execute(
        "SELECT event_type, object_type, object_id, detail_json FROM audit_events "
        "WHERE object_type='circuit_breakers' ORDER BY id"
    ).fetchall()
    assert len(rows) == 3
    assert rows[0]["event_type"] == "CircuitBreakerOpened"
    assert rows[1]["event_type"] == "CircuitBreakerHalfOpened"
    assert rows[2]["event_type"] == "CircuitBreakerClosed"
    for row in rows:
        assert row["object_id"] == dim
        detail = json.loads(row["detail_json"])
        assert "state" in detail


def test_reset_returns_to_closed(conn: sqlite3.Connection) -> None:
    """reset 应把维度重置为 CLOSED 并清空计数。"""
    clock = _Clock()
    breaker = _breaker(conn, clock, failure_threshold=1)
    dim = "provider:deepseek"
    breaker.record_failure(dim)
    assert breaker.state(dim) is BreakerState.OPEN
    breaker.reset(dim)
    assert breaker.state(dim) is BreakerState.CLOSED
    assert breaker.allow(dim) is True


def test_dimensions_isolated(conn: sqlite3.Connection) -> None:
    """不同 dimension 互不影响。"""
    clock = _Clock()
    breaker = _breaker(conn, clock, failure_threshold=1)
    breaker.record_failure("provider:deepseek")
    assert breaker.state("provider:deepseek") is BreakerState.OPEN
    assert breaker.state("provider:openai") is BreakerState.CLOSED
    assert breaker.allow("provider:openai") is True
