"""基础告警/通知测试（S2-08）。

使用临时 SQLite 数据库与可注入时钟，确定性验证脱敏、去重合并、通知失败有限重试
与可观测失败状态，以及各检测器在种子数据上的触发与干净数据上的不触发
（reference §9 精简、§4.3、§12 规则 4/6）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

import pytest

from nexusai_lite import db, secrets
from nexusai_lite.alerts import Alert, AlertManager, AlertSink, Severity
from nexusai_lite.budget import BudgetGuard
from nexusai_lite.records import ErrorCategory

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
    """返回已初始化的临时 SQLite 连接（含 006 迁移）。"""
    db_path = tmp_path / "nexusai.db"
    db.init_db(db_path)
    c = db.connect(db_path)
    yield c
    c.close()


@pytest.fixture
def clock() -> _Clock:
    """返回一个可注入、可推进的时钟。"""
    return _Clock()


class _CollectSink:
    """记录收到的告警的测试通道。"""

    def __init__(self) -> None:
        self.received: list[Alert] = []

    def notify(self, alert: Alert) -> None:
        """记录告警。"""
        self.received.append(alert)


class _SinkUnavailableError(RuntimeError):
    """测试通道不可用异常（用于验证派发失败）。"""


class _FailingSink:
    """每次派发都抛异常的测试通道。"""

    def __init__(self) -> None:
        self.calls = 0

    def notify(self, alert: Alert) -> None:
        """抛异常（计入调用次数）。"""
        self.calls += 1
        raise _SinkUnavailableError


class _SinkProtocolType(Protocol):
    """测试通道的最小接口。"""

    def notify(self, alert: Alert) -> None: ...


def _manager(
    conn: sqlite3.Connection,
    clock: _Clock,
    *,
    sinks: tuple[AlertSink | _SinkProtocolType, ...] = (),
    dedup_window_seconds: int = 300,
) -> AlertManager:
    """构造一个注入时钟的告警管理器。"""
    return AlertManager(
        conn,
        sinks=sinks,
        dedup_window_seconds=dedup_window_seconds,
        now_fn=clock,
    )


def _stored_row(conn: sqlite3.Connection, alert_id: str) -> sqlite3.Row:
    """按 alert_id 读取一行 alerts。"""
    row = conn.execute("SELECT * FROM alerts WHERE alert_id=?", (alert_id,)).fetchone()
    assert row is not None
    return row


def _insert_request(  # noqa: PLR0913 - 测试辅助：请求全字段便于精确播种
    conn: sqlite3.Connection,
    *,
    request_id: str,
    started_at: str,
    status_code: int | None = None,
    error_category: str | None = None,
    provider: str = "deepseek",
) -> None:
    """插入一行 api_request_logs 记录。"""
    conn.execute(
        "INSERT INTO api_request_logs "
        "(request_id, provider, started_at, status_code, error_category) "
        "VALUES (?, ?, ?, ?, ?)",
        (request_id, provider, started_at, status_code, error_category),
    )


# ------------------------------------------------------------- 脱敏

def test_sanitization_redacts_secret_in_stored_and_emitted(
    conn: sqlite3.Connection, clock: _Clock
) -> None:
    """含已登记秘密的 message/detail 应在存储行与派发告警中被脱敏。"""
    secret = "sk-fakeSecretValue0001"  # noqa: S105 - 刻意构造的假密钥用于验证脱敏
    secrets.register_secret(secret)
    sink = _CollectSink()
    manager = _manager(conn, clock, sinks=(sink,))
    alert = manager.raise_alert(
        severity=Severity.CRITICAL,
        code="TEST_SECRET",
        title="泄漏测试",
        message=f"密钥 {secret} 不应出现",
        detail={"token": secret, "nested": {"prompt": f"请使用 {secret}"}},
    )

    assert alert is not None
    assert secret not in alert.message
    assert secret not in str(alert.detail)
    row = _stored_row(conn, alert.alert_id)
    assert secret not in row["message"]
    assert secret not in (row["detail_json"] or "")
    assert secret not in sink.received[0].message


# ------------------------------------------------------------- 去重合并

def test_dedup_within_window_skips(conn: sqlite3.Connection, clock: _Clock) -> None:
    """同 (code, scope) 在去重窗口内第二次 raise_alert 应返回 None 且不新增行。"""
    manager = _manager(conn, clock)
    first = manager.raise_alert(
        severity=Severity.WARNING, code="TEST_DEDUP", title="去重 A", message="m"
    )
    assert first is not None
    second = manager.raise_alert(
        severity=Severity.WARNING, code="TEST_DEDUP", title="去重 B", message="m2"
    )
    assert second is None
    count = conn.execute("SELECT COUNT(*) AS n FROM alerts").fetchone()
    assert count["n"] == 1


def test_dedup_raises_again_after_window(conn: sqlite3.Connection, clock: _Clock) -> None:
    """窗口过后同 (code, scope) 应再次触发并新增一行。"""
    manager = _manager(conn, clock, dedup_window_seconds=300)
    first = manager.raise_alert(
        severity=Severity.WARNING, code="TEST_DEDUP2", title="A", message="m"
    )
    assert first is not None
    clock.advance(301)
    second = manager.raise_alert(
        severity=Severity.WARNING, code="TEST_DEDUP2", title="B", message="m2"
    )
    assert second is not None
    assert second.alert_id != first.alert_id
    count = conn.execute("SELECT COUNT(*) AS n FROM alerts").fetchone()
    assert count["n"] == 2


# ------------------------------------------------------------- 通知失败

def test_sink_failure_retries_and_marks_dispatch_failed(
    conn: sqlite3.Connection, clock: _Clock
) -> None:
    """通道抛异常应有限重试、落库为 dispatch_failed，且管理器不崩溃。"""
    failing = _FailingSink()
    manager = _manager(conn, clock, sinks=(failing,))
    alert = manager.raise_alert(
        severity=Severity.CRITICAL, code="TEST_SINKFAIL", title="T", message="m"
    )
    assert alert is not None
    # 有限重试：最多 3 次。
    assert failing.calls == 3
    row = _stored_row(conn, alert.alert_id)
    assert row["status"] == "dispatch_failed"


def test_sink_success_marks_dispatched(conn: sqlite3.Connection, clock: _Clock) -> None:
    """通道成功时应落 status='dispatched'（可观测已送达）。"""
    manager = _manager(conn, clock, sinks=(_CollectSink(),))
    alert = manager.raise_alert(
        severity=Severity.WARNING, code="TEST_OK", title="T", message="m"
    )
    assert alert is not None
    row = _stored_row(conn, alert.alert_id)
    assert row["status"] == "dispatched"


# ------------------------------------------------------------- 429 限流比率

def test_rate_limit_warning_triggers(conn: sqlite3.Connection, clock: _Clock) -> None:
    """429 比率越软阈值应触发 RATE_LIMIT_RATIO_HIGH（WARNING）。"""
    now = clock().isoformat()
    for i in range(6):
        status = 429 if i < 2 else 200
        _insert_request(conn, request_id=f"r{i}", started_at=now, status_code=status)
    conn.commit()
    manager = _manager(conn, clock)
    alert = manager.detect_rate_limits(conn, minutes=15)
    assert alert is not None
    assert alert.code == "RATE_LIMIT_RATIO_HIGH"
    assert alert.severity is Severity.WARNING


def test_rate_limit_critical_triggers(conn: sqlite3.Connection, clock: _Clock) -> None:
    """429 比率越高严重阈值应触发 CRITICAL。"""
    now = clock().isoformat()
    for i in range(6):
        status = 429 if i < 4 else 200
        _insert_request(conn, request_id=f"r{i}", started_at=now, status_code=status)
    conn.commit()
    manager = _manager(conn, clock)
    alert = manager.detect_rate_limits(conn, minutes=15)
    assert alert is not None
    assert alert.severity is Severity.CRITICAL


def test_rate_limit_no_trigger_on_clean(conn: sqlite3.Connection, clock: _Clock) -> None:
    """样本不足或比率过低时不应触发。"""
    now = clock().isoformat()
    for i in range(6):
        _insert_request(conn, request_id=f"r{i}", started_at=now, status_code=200)
    conn.commit()
    manager = _manager(conn, clock)
    assert manager.detect_rate_limits(conn, minutes=15) is None
    assert manager.detect_rate_limits(conn, minutes=15) is None


# ------------------------------------------------------------- 5xx / 过载

def test_server_errors_triggers(conn: sqlite3.Connection, clock: _Clock) -> None:
    """5xx 比率越阈值应触发 SERVER_ERROR_RATE_HIGH（WARNING）。"""
    now = clock().isoformat()
    statuses = [500, 503, 200, 200, 200, 200]
    for i, status in enumerate(statuses):
        _insert_request(conn, request_id=f"r{i}", started_at=now, status_code=status)
    conn.commit()
    manager = _manager(conn, clock)
    alert = manager.detect_server_errors(conn, minutes=15)
    assert alert is not None
    assert alert.code == "SERVER_ERROR_RATE_HIGH"
    assert alert.severity is Severity.WARNING


def test_server_errors_no_trigger_on_clean(conn: sqlite3.Connection, clock: _Clock) -> None:
    """无 5xx 时不应触发。"""
    now = clock().isoformat()
    for i in range(6):
        _insert_request(conn, request_id=f"r{i}", started_at=now, status_code=200)
    conn.commit()
    manager = _manager(conn, clock)
    assert manager.detect_server_errors(conn, minutes=15) is None


# ------------------------------------------------------------- 熔断打开

def test_open_breaker_triggers(conn: sqlite3.Connection, clock: _Clock) -> None:
    """存在 OPEN 熔断维度应触发 CIRCUIT_BREAKER_OPEN（CRITICAL, scope=dimension）。"""
    now = clock().isoformat()
    conn.execute(
        "INSERT INTO circuit_breakers (dimension, state, last_change_at) "
        "VALUES ('provider:deepseek', 'open', ?)",
        (now,),
    )
    conn.commit()
    manager = _manager(conn, clock)
    alert = manager.detect_open_breakers(conn)
    assert alert is not None
    assert alert.code == "CIRCUIT_BREAKER_OPEN"
    assert alert.severity is Severity.CRITICAL
    assert alert.scope == "provider:deepseek"


def test_open_breaker_no_trigger_on_clean(conn: sqlite3.Connection, clock: _Clock) -> None:
    """无 OPEN 熔断（如 CLOSED）不应触发。"""
    now = clock().isoformat()
    conn.execute(
        "INSERT INTO circuit_breakers (dimension, state, last_change_at) "
        "VALUES ('provider:deepseek', 'closed', ?)",
        (now,),
    )
    conn.commit()
    manager = _manager(conn, clock)
    assert manager.detect_open_breakers(conn) is None


# ------------------------------------------------------------- 预算阈值

def test_budget_hard_limit_triggers(conn: sqlite3.Connection, clock: _Clock) -> None:
    """账户用量超过硬上限应触发 BUDGET_HARD_LIMIT（CRITICAL）。"""
    guard = BudgetGuard(conn)
    guard.set_limits("model:x", hard_limit=1.0)
    conn.execute(
        "INSERT INTO cost_records "
        "(request_id, provider, model, cost_status, actual_cost, created_at) "
        "VALUES ('c1', 'deepseek', 'x', 'actual', 2.0, ?)",
        (clock().isoformat(),),
    )
    conn.commit()
    manager = _manager(conn, clock)
    alert = manager.detect_budget_thresholds(conn)
    assert alert is not None
    assert alert.code == "BUDGET_HARD_LIMIT"
    assert alert.severity is Severity.CRITICAL
    assert alert.scope == "model:x"


def test_budget_soft_threshold_triggers(conn: sqlite3.Connection, clock: _Clock) -> None:
    """账户用量越过软阈值（未达硬上限）应触发 BUDGET_SOFT_THRESHOLD（WARNING）。"""
    guard = BudgetGuard(conn)
    guard.set_limits("model:y", soft_limit=1.0)
    conn.execute(
        "INSERT INTO cost_records "
        "(request_id, provider, model, cost_status, actual_cost, created_at) "
        "VALUES ('c2', 'deepseek', 'y', 'actual', 2.0, ?)",
        (clock().isoformat(),),
    )
    conn.commit()
    manager = _manager(conn, clock)
    alert = manager.detect_budget_thresholds(conn)
    assert alert is not None
    assert alert.code == "BUDGET_SOFT_THRESHOLD"
    assert alert.severity is Severity.WARNING


def test_budget_no_trigger_on_clean(conn: sqlite3.Connection, clock: _Clock) -> None:
    """无账户 / 未超限时不应触发。"""
    manager = _manager(conn, clock)
    assert manager.detect_budget_thresholds(conn) is None


# ------------------------------------------------------------- 任务 UNKNOWN

def test_unknown_task_triggers(conn: sqlite3.Connection, clock: _Clock) -> None:
    """存在 UNKNOWN 任务应触发 TASK_UNKNOWN（CRITICAL）并携带人工接管提示。"""
    conn.execute(
        "INSERT INTO tasks (task_id, status, created_at) VALUES ('tk1', 'UNKNOWN', ?)",
        (clock().isoformat(),),
    )
    conn.commit()
    manager = _manager(conn, clock)
    alert = manager.detect_unknown_tasks(conn)
    assert alert is not None
    assert alert.code == "TASK_UNKNOWN"
    assert alert.severity is Severity.CRITICAL
    assert alert.event_id == "tk1"
    assert alert.scope == "task:tk1"
    assert "人工接管" in alert.message


def test_unknown_task_no_trigger_on_clean(conn: sqlite3.Connection, clock: _Clock) -> None:
    """无 UNKNOWN 任务（如 SUCCEEDED）不应触发。"""
    conn.execute(
        "INSERT INTO tasks (task_id, status, created_at) VALUES ('tk2', 'SUCCEEDED', ?)",
        (clock().isoformat(),),
    )
    conn.commit()
    manager = _manager(conn, clock)
    assert manager.detect_unknown_tasks(conn) is None


# ------------------------------------------------------------- 认证失败

def test_auth_failure_triggers(conn: sqlite3.Connection, clock: _Clock) -> None:
    """存在 AUTHENTICATION_ERROR 记录应触发 AUTHENTICATION_FAILURE（CRITICAL）。"""
    now = clock().isoformat()
    _insert_request(
        conn,
        request_id="auth1",
        started_at=now,
        error_category=ErrorCategory.AUTHENTICATION_ERROR.value,
    )
    conn.commit()
    manager = _manager(conn, clock)
    alert = manager.detect_auth_failures(conn, minutes=15)
    assert alert is not None
    assert alert.code == "AUTHENTICATION_FAILURE"
    assert alert.severity is Severity.CRITICAL
    assert alert.event_id == "auth1"


def test_auth_failure_no_trigger_on_clean(conn: sqlite3.Connection, clock: _Clock) -> None:
    """无认证失败记录时不应触发。"""
    now = clock().isoformat()
    _insert_request(
        conn,
        request_id="auth2",
        started_at=now,
        error_category=ErrorCategory.RATE_LIMITED.value,
    )
    conn.commit()
    manager = _manager(conn, clock)
    assert manager.detect_auth_failures(conn, minutes=15) is None


# ------------------------------------------------------------- 聚合扫描

def test_scan_and_alert_returns_raised(conn: sqlite3.Connection, clock: _Clock) -> None:
    """scan_and_alert 应返回全部新触发的告警；干净库返回空列表。"""
    manager = _manager(conn, clock)
    assert manager.scan_and_alert(conn, minutes=15) == []
    now = clock().isoformat()
    for i in range(6):
        _insert_request(
            conn,
            request_id=f"s{i}",
            started_at=now,
            status_code=429 if i < 4 else 200,
        )
    conn.execute(
        "INSERT INTO circuit_breakers (dimension, state, last_change_at) "
        "VALUES ('provider:deepseek', 'open', ?)",
        (now,),
    )
    conn.commit()
    raised = manager.scan_and_alert(conn, minutes=15)
    codes = {a.code for a in raised}
    assert "RATE_LIMIT_RATIO_HIGH" in codes
    assert "CIRCUIT_BREAKER_OPEN" in codes
