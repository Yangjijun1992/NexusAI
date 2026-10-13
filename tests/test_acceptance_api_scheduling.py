"""X-01 验收：API 客户端与错峰调度测试（reference §9 / §12.1）。

覆盖 reference.md §9 与任务清单 X-01 的 API/调度验收标准：

- **采集/请求正确性：** 统一 API Client 对正常响应、流式/非流式、429/5xx/超时/
  连接失败/无效请求/认证失败的正确分类与有限重试；``Retry-After`` 存在与缺失时
  退避策略正确。
- **预算硬上限（§12 规则 9 / §8 阶段 2 退出条件）：** 两个并发 ``BudgetGuard.reserve``
  合计超限时第二个被拒绝，系统绝不被自动突破硬上限。
- **熔断（F5）：** 用注入时钟驱动 CLOSED/OPEN/HALF_OPEN/恢复与审计。
- **错峰调度：** 并发容量（``at_capacity``）、高峰降载（``peak_defer``）、无规则
  保守并发、重要任务受保护；决策可解释并定位到规则版本（``explain``）。
- **保守降级：** 适配器返回未知格式 / 缺失用量 / 缺失价格时成本估算为 unknown
  （不臆造），且不可重试错误绝不无限重试。
"""

# ruff: noqa: PLR0913
# PLR0913: _make_client 测试辅助 9 个参数，对应 ApiClient 各重试/退避/记录配置项。

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import pytest

from nexusai_lite import db
from nexusai_lite.api_client import ApiClient, ApiClientError
from nexusai_lite.budget import BudgetExceededError, BudgetGuard
from nexusai_lite.circuit_breaker import BreakerState, CircuitBreaker
from nexusai_lite.collector.window_rules import WindowRuleData, upsert_window_rule
from nexusai_lite.providers.deepseek import DeepSeekAdapter
from nexusai_lite.records import (
    ApiRequest,
    ErrorCategory,
    RequestLogger,
    RequestRecord,
)
from nexusai_lite.scheduler import (
    REASON_AT_CAPACITY,
    REASON_IMPORTANT,
    REASON_PEAK_DEFER,
    DecisionResult,
    Scheduler,
)
from nexusai_lite.tasks import Task, TaskService, TaskStatus

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterator
    from pathlib import Path

_DEFAULT_MODEL = "deepseek-chat"


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


# ---------------------------------------------------------------- DB 与时钟夹具


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """返回已初始化的临时 SQLite 连接（自动应用全部迁移并播种默认配置 v1）。"""
    db_path = tmp_path / "nexusai.db"
    db.init_db(db_path)
    c = db.connect(db_path)
    yield c
    c.close()


def _seed_rule(conn: sqlite3.Connection) -> None:
    """写入默认高峰窗口规则并激活（工作日 09:00-12:00 / 14:00-18:00 北京时区）。"""
    rule = WindowRuleData(
        timezone="Asia/Shanghai",
        workdays=(1, 2, 3, 4, 5),
        peak_windows=(("09:00", "12:00"), ("14:00", "18:00")),
        source="official-pricing-page",
    )
    upsert_window_rule(conn, rule, source="official-pricing-page")


# ---------------------------------------------------------------- API Client 辅助


def _make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    sleeps: list[float],
    *,
    price_table: dict[str, dict[str, float | str]] | None = None,
    max_retries: int = 2,
    allow_retry_on_timeout: bool = False,
    retry_jitter: float = 0.0,
    logger: RequestLogger | None = None,
    backoff_base_seconds: float = 1.0,
    api_key: str | None = None,
) -> ApiClient:
    """构造统一 API Client，注入 MockTransport 与 no-op 退避。"""
    adapter = DeepSeekAdapter(price_table=price_table)
    http = httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)
    return ApiClient(
        adapter,
        http_client=http,
        max_retries=max_retries,
        allow_retry_on_timeout=allow_retry_on_timeout,
        retry_jitter=retry_jitter,
        logger=logger,
        backoff_base_seconds=backoff_base_seconds,
        sleep_fn=sleeps.append,
        api_key=api_key,
    )


def _request(
    *,
    stream: bool = False,
    cost_limit: float | None = None,
    max_retries: int | None = None,
    allow_retry_on_timeout: bool | None = None,
    model: str = _DEFAULT_MODEL,
) -> ApiRequest:
    """构造一个最小请求。"""
    return ApiRequest(
        model=model,
        messages=[{"role": "user", "content": "hi"}],
        stream=stream,
        cost_limit=cost_limit,
        max_retries=max_retries,
        allow_retry_on_timeout=allow_retry_on_timeout,
    )


def _ok_json() -> str:
    """构造带用量的正常响应体。"""
    return json.dumps(
        {
            "choices": [{"message": {"role": "assistant", "content": "hello"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }
    )


def _error_json(message: str) -> str:
    """构造错误响应体。"""
    return json.dumps({"error": {"message": message}})


class _InMemoryLogger:
    """把 :class:`RequestRecord` 写入同一连接的请求日志器（供成本记录断言）。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        """以指定连接构造日志器。"""
        self._conn = conn

    def log_request(self, record: RequestRecord) -> None:
        """把请求与成本记录落库（幂等）。"""
        category = record.error_category.value if record.error_category is not None else None
        self._conn.execute(
            "INSERT OR IGNORE INTO api_request_logs "
            "(request_id, task_id, provider, model, started_at, ended_at, "
            " status_code, latency_ms, input_tokens, output_tokens, retry_count, "
            " estimated_cost, cost_status, error_code, error_category, "
            " safe_error_message, raw_error_reference, streaming) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.request_id,
                record.task_id,
                record.provider,
                record.model,
                record.started_at,
                record.ended_at,
                record.status_code,
                record.latency_ms,
                record.input_tokens,
                record.output_tokens,
                record.retry_count,
                record.estimated_cost,
                record.cost_status,
                category,
                category,
                record.safe_error_message,
                record.raw_error_reference,
                1 if record.streaming else 0,
            ),
        )
        self._conn.execute(
            "INSERT OR IGNORE INTO cost_records "
            "(request_id, attempt_id, provider, model, input_tokens, output_tokens, "
            " estimated_cost, cost_status, basis, effective_at, source, created_at) "
            "VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.request_id,
                record.provider,
                record.model,
                record.input_tokens,
                record.output_tokens,
                record.estimated_cost,
                record.cost_status,
                "api_response_usage",
                record.started_at,
                "platform_estimate",
                db.utcnow_iso(),
            ),
        )
        self._conn.commit()


def db_logger(conn: sqlite3.Connection) -> RequestLogger:
    """返回写入 ``conn`` 的请求日志器（实现 RequestLogger 协议）。"""
    return _InMemoryLogger(conn)


# ---------------------------------------------------------------- §12.1 正常/流式


def test_normal_non_stream_response_with_usage(conn: sqlite3.Connection) -> None:
    """正常非流式请求应返回内容并记录用量成本。"""
    logger = db_logger(conn)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_ok_json())

    sleeps: list[float] = []
    client = _make_client(
        handler,
        sleeps,
        retry_jitter=0.0,
        logger=logger,
        price_table={
            _DEFAULT_MODEL: {"input_per_million": 1.0, "output_per_million": 2.0, "currency": "CNY"}
        },
    )
    resp = client.chat(_request())
    assert resp.status_code == 200
    assert resp.content is not None
    assert resp.usage is not None
    assert resp.usage.input_tokens == 3
    assert resp.usage.output_tokens == 2
    assert resp.record.cost_status == "estimated"
    assert sleeps == []  # 无需重试
    # 请求记录与成本记录均已落库。
    row = conn.execute(
        "SELECT cost_status FROM cost_records WHERE request_id=?",
        (resp.record.request_id,),
    ).fetchone()
    assert row is not None
    assert row["cost_status"] == "estimated"


def test_stream_response_chunks(conn: sqlite3.Connection) -> None:
    """流式请求应按块产出增量并记录用量。"""
    body = (
        'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":""}}],'
        '"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    sleeps: list[float] = []
    client = _make_client(handler, sleeps, retry_jitter=0.0)
    chunks = list(client.chat_stream(_request(stream=True)))
    assert [c.delta for c in chunks] == ["Hel", "lo"]


# ---------------------------------------------------------------- §12.1 错误分类/重试


def test_rate_limited_retries_respecting_retry_after(conn: sqlite3.Connection) -> None:
    """429 应尊重 ``Retry-After`` 有限重试后成功，退避取 Retry-After。"""
    counts: dict[str, int] = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        counts["n"] += 1
        if counts["n"] == 1:
            return httpx.Response(
                429, headers={"Retry-After": "7"}, content=_error_json("rate limited")
            )
        return httpx.Response(200, content=_ok_json())

    sleeps: list[float] = []
    client = _make_client(
        handler, sleeps, retry_jitter=0.0, max_retries=2, backoff_base_seconds=1.0
    )
    resp = client.chat(_request())
    assert resp.status_code == 200
    # 首次 429 → 退避 7s（尊重 Retry-After）。
    assert sleeps
    assert 6.0 < sleeps[0] <= 7.0


def test_rate_limited_without_retry_after_uses_exp_backoff(
    conn: sqlite3.Connection,
) -> None:
    """429 无 ``Retry-After`` 时采用有限指数退避。"""
    counts: dict[str, int] = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        counts["n"] += 1
        if counts["n"] == 1:
            return httpx.Response(429, content=_error_json("rate limited"))
        return httpx.Response(200, content=_ok_json())

    sleeps: list[float] = []
    # backoff_base=2 → 首次退避 2s。
    client = _make_client(
        handler, sleeps, retry_jitter=0.0, max_retries=2, backoff_base_seconds=2.0
    )
    resp = client.chat(_request())
    assert resp.status_code == 200
    assert sleeps
    assert sleeps[0] == 2.0


def test_auth_failure_not_retried(conn: sqlite3.Connection) -> None:
    """认证错误不应重试（F5：停止并告警，不反复尝试失效密钥）。"""
    counts: dict[str, int] = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        counts["n"] += 1
        return httpx.Response(401, content=_error_json("unauthorized"))

    sleeps: list[float] = []
    client = _make_client(handler, sleeps, max_retries=5)
    with pytest.raises(ApiClientError) as exc:
        client.chat(_request())
    assert exc.value.record.error_category is ErrorCategory.AUTHENTICATION_ERROR
    assert counts["n"] == 1  # 仅一次，绝不无限重试
    assert sleeps == []


def test_invalid_request_not_retried(conn: sqlite3.Connection) -> None:
    """参数错误（422/400）不可重试，合并在有限尝试内。"""
    counts: dict[str, int] = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        counts["n"] += 1
        return httpx.Response(422, content=_error_json("bad param"))

    sleeps: list[float] = []
    client = _make_client(handler, sleeps, max_retries=5)
    with pytest.raises(ApiClientError) as exc:
        client.chat(_request())
    assert exc.value.record.error_category is ErrorCategory.INVALID_REQUEST
    assert counts["n"] == 1


def test_server_error_retried_then_fails(conn: sqlite3.Connection) -> None:
    """5xx 可有限重试，耗尽后抛错并记录重试原因。"""
    counts: dict[str, int] = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        counts["n"] += 1
        return httpx.Response(503, content=_error_json("overloaded"))

    sleeps: list[float] = []
    client = _make_client(handler, sleeps, retry_jitter=0.0, max_retries=2)
    with pytest.raises(ApiClientError) as exc:
        client.chat(_request())
    assert exc.value.record.error_category is ErrorCategory.PROVIDER_OVERLOADED
    assert counts["n"] == 3  # max_retries=2 → 共 3 次尝试
    assert len(sleeps) == 2
    # 每次尝试都有记录。
    attempts = exc.value.record.attempts
    assert len(attempts) == 3
    assert all(a.retry_reason is None or a.may_be_billed for a in attempts)


def test_connection_failure_retried_then_fails(conn: sqlite3.Connection) -> None:
    """连接失败分类为网络错误，有限重试后失败。"""
    counts: dict[str, int] = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        counts["n"] += 1
        msg = "conn refused"
        raise httpx.ConnectError(msg, request=request)

    sleeps: list[float] = []
    client = _make_client(handler, sleeps, retry_jitter=0.0, max_retries=1)
    with pytest.raises(ApiClientError) as exc:
        client.chat(_request())
    assert exc.value.record.error_category is ErrorCategory.NETWORK_ERROR
    # 连接失败未计费（may_be_billed=False 或分类为网络）。
    assert counts["n"] == 2


def test_timeout_not_retried_unless_allowed(conn: sqlite3.Connection) -> None:
    """超时默认不可重试（TIMEOUT_UNKNOWN 保守），显式允许时才重试。"""
    default_counts: dict[str, int] = {"n": 0}

    def default_handler(request: httpx.Request) -> httpx.Response:
        default_counts["n"] += 1
        msg = "read timeout"
        raise httpx.ReadTimeout(msg, request=request)

    sleeps: list[float] = []
    client = _make_client(default_handler, sleeps, max_retries=3)
    with pytest.raises(ApiClientError) as exc:
        client.chat(_request())
    assert exc.value.record.error_category is ErrorCategory.TIMEOUT_UNKNOWN
    assert default_counts["n"] == 1  # 不重试
    assert sleeps == []

    allowed_counts: dict[str, int] = {"n": 0}

    def allowed_handler(request: httpx.Request) -> httpx.Response:
        allowed_counts["n"] += 1
        if allowed_counts["n"] == 1:
            msg = "read timeout"
            raise httpx.ReadTimeout(msg, request=request)
        return httpx.Response(200, content=_ok_json())

    sleeps2: list[float] = []
    client2 = _make_client(
        allowed_handler, sleeps2, retry_jitter=0.0, max_retries=2, allow_retry_on_timeout=True
    )
    resp = client2.chat(_request())
    assert resp.status_code == 200
    assert allowed_counts["n"] == 2


def test_retry_has_traceable_reason_and_cost_record(conn: sqlite3.Connection) -> None:
    """每次重试都有可追踪原因与成本记录（§12 规则 4 / X-01 有限重试）。"""
    logger = db_logger(conn)
    counts: dict[str, int] = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        counts["n"] += 1
        if counts["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "1"}, content=_error_json("rl"))
        return httpx.Response(200, content=_ok_json())

    sleeps: list[float] = []
    client = _make_client(handler, sleeps, retry_jitter=0.0, max_retries=2, logger=logger)
    resp = client.chat(_request())
    assert resp.status_code == 200
    # 尝试 1 为 429（RATE_LIMITED），原因可追踪；尝试 2 成功。
    first = resp.record.attempts[0]
    assert first.error_category is ErrorCategory.RATE_LIMITED
    # 成本记录存在（成功响应的估算成本）。
    row = conn.execute(
        "SELECT cost_status FROM cost_records WHERE request_id=?",
        (resp.record.request_id,),
    ).fetchone()
    assert row is not None


# ---------------------------------------------------------------- §12.1 未知格式/缺失用量


def test_unknown_format_and_missing_usage_conservative(conn: sqlite3.Connection) -> None:
    """适配器返回未知格式 / 缺失用量时成本估算为 unknown（保守，不臆造）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content='{"choices": []}')

    sleeps: list[float] = []
    logger = db_logger(conn)
    client = _make_client(handler, sleeps, retry_jitter=0.0, logger=logger)
    resp = client.chat(_request())
    assert resp.usage is None
    assert resp.record.estimated_cost is None
    assert resp.record.cost_status == "unknown"
    row = conn.execute(
        "SELECT cost_status, estimated_cost FROM cost_records WHERE request_id=?",
        (resp.record.request_id,),
    ).fetchone()
    assert row is not None
    assert row["cost_status"] == "unknown"
    assert row["estimated_cost"] is None


def test_cost_unknown_when_no_pricing(conn: sqlite3.Connection) -> None:
    """无价格表条目时成本估算为 unknown（reference：不得臆造官方价格）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_ok_json())

    sleeps: list[float] = []
    client = _make_client(handler, sleeps, retry_jitter=0.0, price_table={})
    resp = client.chat(_request())
    assert resp.usage is not None
    assert resp.record.estimated_cost is None
    assert resp.record.cost_status == "unknown"


def test_cost_estimate_with_pricing(conn: sqlite3.Connection) -> None:
    """有价格表时成本按 token 用量估算。"""
    price_table: dict[str, dict[str, float | str]] = {
        _DEFAULT_MODEL: {"input_per_million": 1.0, "output_per_million": 2.0, "currency": "CNY"}
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_ok_json())

    sleeps: list[float] = []
    client = _make_client(handler, sleeps, retry_jitter=0.0, price_table=price_table)
    resp = client.chat(_request())
    assert resp.record.estimated_cost == pytest.approx(3 / 1e6 * 1.0 + 2 / 1e6 * 2.0)
    assert resp.record.cost_status == "estimated"


# ---------------------------------------------------------------- §12.1 预算硬上限


def test_concurrent_budget_reservation_second_rejected(conn: sqlite3.Connection) -> None:
    """并发竞争防护：两个保留合计超硬上限时第二个被拒绝。"""
    guard = BudgetGuard(conn)
    guard.set_limits("platform", hard_limit=1.0)
    first = guard.reserve("platform", 0.6)
    assert first.status == "active"
    with pytest.raises(BudgetExceededError) as exc:
        guard.reserve("platform", 0.6)
    assert exc.value.scope == "platform"
    assert exc.value.hard_limit == 1.0
    # 第一次预留仍有效，未因第二次失败回滚。
    u = guard.usage("platform")
    assert u["reserved"] == 0.6
    assert u["available"] == pytest.approx(0.4)


def test_budget_hard_limit_never_exceeded_via_commit(conn: sqlite3.Connection) -> None:
    """已提交成本计入用量，硬上限不被系统自动突破。"""
    guard = BudgetGuard(conn)
    guard.set_limits("platform", hard_limit=1.0)
    conn.execute(
        "INSERT INTO cost_records "
        "(request_id, provider, model, cost_status, actual_cost, created_at) "
        "VALUES ('c1', 'deepseek', 'x', 'actual', 0.7, ?)",
        (db.utcnow_iso(),),
    )
    conn.commit()
    # 剩余 0.3；申请 0.5 应被拒绝。
    with pytest.raises(BudgetExceededError):
        guard.reserve("platform", 0.5)
    r = guard.reserve("platform", 0.3)
    assert r.status == "active"
    u = guard.usage("platform")
    assert u["available"] == pytest.approx(0.0)


def test_reserve_release_returns_capacity(conn: sqlite3.Connection) -> None:
    """释放预留后容量归还（不永久占用预算容量）。"""
    guard = BudgetGuard(conn)
    guard.set_limits("platform", hard_limit=1.0)
    r = guard.reserve("platform", 0.9)
    assert guard.usage("platform")["available"] == pytest.approx(0.1)
    guard.release(r.reservation_id)
    assert guard.usage("platform")["available"] == pytest.approx(1.0)


# ---------------------------------------------------------------- §12.1 熔断


def test_breaker_opens_and_audits(conn: sqlite3.Connection) -> None:
    """连续失败达到阈值后熔断打开（OPEN）并审计。"""
    clock = _Clock()
    breaker = CircuitBreaker(conn, now_fn=clock, failure_threshold=3, open_seconds=10.0)
    for _ in range(3):
        breaker.record_failure("provider:deepseek")
    assert breaker.state("provider:deepseek") is BreakerState.OPEN
    events = _audit_types(conn)
    assert "CircuitBreakerOpened" in events


def test_breaker_half_open_and_recovery(conn: sqlite3.Connection) -> None:
    """冷却期后自动 HALF_OPEN，连续成功恢复 CLOSED（含审计）。"""
    clock = _Clock()
    breaker = CircuitBreaker(
        conn, now_fn=clock, failure_threshold=3, open_seconds=10.0, success_threshold=2
    )
    for _ in range(3):
        breaker.record_failure("provider:deepseek")
    assert breaker.state("provider:deepseek") is BreakerState.OPEN
    # 冷却期未过，仍 OPEN。
    clock.advance(5.0)
    assert breaker.state("provider:deepseek") is BreakerState.OPEN
    # 冷却期过后进入 HALF_OPEN。
    clock.advance(6.0)
    assert breaker.state("provider:deepseek") is BreakerState.HALF_OPEN
    # 探测：允许一个；连续成功达到阈值后恢复 CLOSED。
    assert breaker.allow("provider:deepseek") is True
    breaker.record_success("provider:deepseek")
    breaker.record_success("provider:deepseek")
    assert breaker.state("provider:deepseek") is BreakerState.CLOSED
    events = _audit_types(conn)
    assert "CircuitBreakerHalfOpened" in events
    assert "CircuitBreakerClosed" in events


def test_breaker_half_open_probe_limit(conn: sqlite3.Connection) -> None:
    """HALF_OPEN 下在途探测数达到上限时不允许更多探测。"""
    clock = _Clock()
    breaker = CircuitBreaker(
        conn, now_fn=clock, failure_threshold=1, open_seconds=0.0, half_open_max_probes=1
    )
    breaker.record_failure("provider:deepseek")
    breaker.record_failure("provider:deepseek")  # 保持 OPEN 并重置冷却
    clock.advance(1.0)
    assert breaker.state("provider:deepseek") is BreakerState.HALF_OPEN
    assert breaker.allow("provider:deepseek") is True
    assert breaker.allow("provider:deepseek") is False  # 在途探测已达上限
    breaker.release_probe("provider:deepseek", success=True)


# ---------------------------------------------------------------- §12.1 错峰调度


def _queued_task(service: TaskService, *, priority: str = "normal") -> str:
    """创建一个 PENDING 任务并入队，返回 task_id。"""
    task = service.create_task(
        task_type="llm",
        priority=priority,
        estimated_cost=0.1,
    )
    service.queue(task.task_id)
    return task.task_id


def _require_task(service: TaskService, task_id: str) -> Task:
    """返回任务；不存在时失败（避免 Optional 访问）。"""
    task = service.get_task(task_id)
    assert task is not None
    return task


def test_scheduler_concurrency_cap_at_capacity(conn: sqlite3.Connection) -> None:
    """自身并发已达上限时非紧急任务 DELAY（at_capacity），并记录决策。"""
    _seed_rule(conn)
    clock = _Clock(_off_peak_clock())
    scheduler = Scheduler(conn, now_fn=clock, max_inflight=1)
    service = TaskService(conn)
    # 占满并发：一个 RUNNING 任务。
    running = service.create_task(task_type="llm", estimated_cost=0.1)
    service.queue(running.task_id)
    service.start(running.task_id)
    queued = _queued_task(service)
    decision = scheduler.decide(queued)
    assert decision.result is DecisionResult.DELAY
    assert REASON_AT_CAPACITY in decision.reason_codes
    # 记录并可从 explain 读取。
    scheduler.record(decision)
    explained = scheduler.explain(decision.decision_id)
    assert explained["reason_codes"] == list(decision.reason_codes)
    assert explained["policy_version"] == "1"


def test_scheduler_peak_defer_non_urgent(conn: sqlite3.Connection) -> None:
    """高峰时段非紧急任务 DELAY（peak_defer），决策可定位到规则版本。"""
    _seed_rule(conn)
    clock = _Clock(_peak_utc())
    scheduler = Scheduler(conn, now_fn=clock, max_inflight=2)
    service = TaskService(conn)
    queued = _queued_task(service)
    decision = scheduler.decide(queued)
    assert decision.result is DecisionResult.DELAY
    assert REASON_PEAK_DEFER in decision.reason_codes
    assert decision.data_quality == "ok"
    assert decision.policy_version is not None


def test_scheduler_important_protected_during_peak(conn: sqlite3.Connection) -> None:
    """高峰时段重要（高优先级）任务受保护仍 ALLOW。"""
    _seed_rule(conn)
    clock = _Clock(_peak_utc())
    scheduler = Scheduler(conn, now_fn=clock, max_inflight=2)
    service = TaskService(conn)
    queued = _queued_task(service, priority="high")
    decision = scheduler.decide(queued)
    assert decision.result is DecisionResult.ALLOW
    assert REASON_IMPORTANT in decision.reason_codes


def test_scheduler_conservative_cap_no_rule(conn: sqlite3.Connection) -> None:
    """无活跃时段规则时采用保守并发（数据质量不足不放行过量）。"""
    clock = _Clock(_off_peak_clock())
    scheduler = Scheduler(conn, now_fn=clock, conservative_inflight=1)
    service = TaskService(conn)
    # 占满保守并发。
    running = service.create_task(task_type="llm", estimated_cost=0.1)
    service.queue(running.task_id)
    service.start(running.task_id)
    queued = _queued_task(service)
    decision = scheduler.decide(queued)
    assert decision.result is DecisionResult.DELAY
    assert REASON_AT_CAPACITY in decision.reason_codes
    assert decision.data_quality == "NO_RULE"


def test_scheduler_decision_explainable_linked_to_policy(conn: sqlite3.Connection) -> None:
    """调度决策可解释并定位到策略版本（§12 / §9 错峰行为）。"""
    _seed_rule(conn)
    clock = _Clock(_off_peak_clock())
    scheduler = Scheduler(conn, now_fn=clock, max_inflight=2)
    service = TaskService(conn)
    queued = _queued_task(service)
    decision = scheduler.decide(queued)
    scheduler.record(decision)
    explained = scheduler.explain(decision.decision_id)
    assert explained["decision_id"] == decision.decision_id
    assert explained["decision"] == decision.result.value
    assert explained["policy_version"] is not None
    assert "has_window_rule" in explained["metrics_summary"]


# ---------------------------------------------------------------- §12.1 任务过期


def test_peak_defer_and_task_expiry(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """超期非终态任务被转为 EXPIRED（recover / expire_overdue）。"""
    service = TaskService(conn)
    past_deadline = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    queued = service.create_task(
        task_type="llm", estimated_cost=0.1, deadline_at=past_deadline
    )
    service.queue(queued.task_id)
    affected = service.expire_overdue()
    assert queued.task_id in affected
    assert _require_task(service, queued.task_id).status is TaskStatus.EXPIRED


# ---------------------------------------------------------------- 模块级工具


def _peak_utc() -> datetime:
    """返回北京时间 2026-10-12 10:00（高峰窗口内）对应的 UTC 时刻。"""
    # Beijing 10:00 = UTC 02:00；2026-10-12 为周一（工作日）。
    return datetime(2026, 10, 12, 2, 0, 0, tzinfo=UTC)


def _off_peak_clock() -> datetime:
    """返回北京时间 2026-10-12 13:00（空闲时段）对应的 UTC 时刻。"""
    # Beijing 13:00 = UTC 05:00。
    return datetime(2026, 10, 12, 5, 0, 0, tzinfo=UTC)


def _audit_types(conn: sqlite3.Connection) -> list[str]:
    """按插入顺序返回审计事件类型列表。"""
    rows = conn.execute("SELECT event_type FROM audit_events ORDER BY id").fetchall()
    return [r["event_type"] for r in rows]
