"""统一 API Client 测试（S1-03）。

使用 ``httpx.MockTransport`` + 注入的 ``http_client`` 与 no-op ``sleep_fn``，
完全不依赖真实网络或不真实延迟，保证确定性。
"""

# ruff: noqa: PLR0913
# PLR0913: _make_client 测试辅助 8 个参数，对应 ApiClient 各重试/退避配置项。

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

import httpx
import pytest

from nexusai_lite.api_client import ApiClient, ApiClientError, StreamChunk

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
from nexusai_lite.providers.deepseek import DeepSeekAdapter
from nexusai_lite.records import (
    ApiRequest,
    ErrorCategory,
    RequestLogger,
    RequestRecord,
    Usage,
)

_DEFAULT_MODEL = "deepseek-chat"


class RequestLoggerSpy(RequestLogger):
    """记录调用总数的请求记录器测试替身。"""

    def __init__(self) -> None:
        self.records: list[RequestRecord] = []

    def log_request(self, record: RequestRecord) -> None:
        self.records.append(record)


def _request(
    *,
    stream: bool = False,
    cost_limit: float | None = None,
    max_retries: int | None = None,
    allow_retry_on_timeout: bool | None = None,
) -> ApiRequest:
    return ApiRequest(
        model=_DEFAULT_MODEL,
        messages=[{"role": "user", "content": "hi"}],
        stream=stream,
        cost_limit=cost_limit,
        max_retries=max_retries,
        allow_retry_on_timeout=allow_retry_on_timeout,
    )


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
) -> ApiClient:
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
    )


def _ok_json() -> str:
    return json.dumps(
        {
            "choices": [{"message": {"role": "assistant", "content": "hello"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }
    )


def _error_json(message: str) -> str:
    return json.dumps({"error": {"message": message}})


# ------------------------------------------------------------------ 成功路径


class TestChatSuccess:
    """非流式成功路径。"""

    def test_success_non_stream(self) -> None:
        """成功：record 字段正确（retry_count=0、tokens、成本 unknown）。"""
        sleeps: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_ok_json())

        client = _make_client(handler, sleeps)
        resp = client.chat(_request())

        assert resp.status_code == 200
        assert resp.usage == Usage(input_tokens=3, output_tokens=2, total_tokens=5)
        assert resp.content == _ok_json()
        record = resp.record
        assert record.retry_count == 0
        assert record.input_tokens == 3
        assert record.output_tokens == 2
        assert record.estimated_cost is None
        assert record.cost_status == "unknown"
        assert record.latency_ms >= 0
        assert record.status_code == 200
        assert record.error_category is None
        assert len(record.attempts) == 1
        assert record.attempts[0].may_be_billed is True
        assert sleeps == []

    def test_success_sets_request_id_header(self) -> None:
        """X-Request-Id 应设置在请求头且每次调用不同。"""
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, content=_ok_json())

        client = _make_client(handler, [])
        client.chat(_request())
        client.chat(_request())
        assert captured[0].headers["X-Request-Id"]
        assert captured[1].headers["X-Request-Id"]
        assert captured[0].headers["X-Request-Id"] != captured[1].headers["X-Request-Id"]

    def test_success_sends_bearer_auth_when_key_set(self) -> None:
        """设置 api_key 时应发送 Authorization Bearer。"""
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, content=_ok_json())

        adapter = DeepSeekAdapter()
        http = httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)
        client = ApiClient(
            adapter,
            http_client=http,
            api_key="sk-test-secret",
            sleep_fn=lambda d: None,
        )
        client.chat(_request())
        assert captured[0].headers["Authorization"] == "Bearer sk-test-secret"


# ------------------------------------------------------------------ 重试


class TestChatRetry:
    """有限重试。"""

    def test_retry_429_then_success_with_retry_after(self) -> None:
        """429 + Retry-After 后重试成功：sleep ~0.5s，attempts=2。"""
        sleeps: list[float] = []
        counts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            counts["n"] += 1
            if counts["n"] == 1:
                return httpx.Response(
                    429, headers={"Retry-After": "0.5"}, content=_error_json("rate limited")
                )
            return httpx.Response(200, content=_ok_json())

        client = _make_client(handler, sleeps, retry_jitter=0.0)
        resp = client.chat(_request())

        assert sleeps == [0.5]
        assert resp.record.retry_count == 1
        assert len(resp.record.attempts) == 2
        assert resp.record.attempts[0].status_code == 429
        assert resp.record.attempts[0].error_category == ErrorCategory.RATE_LIMITED
        assert resp.record.attempts[0].may_be_billed is True
        assert resp.record.attempts[1].status_code == 200

    def test_retry_exhausted_429_raises(self) -> None:
        """429 重试耗尽抛 ApiClientError，最终分类 RATE_LIMITED。"""
        sleeps: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                429, headers={"Retry-After": "1"}, content=_error_json("slow down")
            )

        client = _make_client(handler, sleeps, max_retries=2, backoff_base_seconds=1.0)
        with pytest.raises(ApiClientError) as excinfo:
            client.chat(_request())
        err: ApiClientError = excinfo.value
        assert err.record.error_category == ErrorCategory.RATE_LIMITED
        assert err.record.retry_count == 2
        assert len(err.record.attempts) == 3
        assert len(sleeps) == 2  # 两次重试退避

    def test_no_retry_on_401(self) -> None:
        """401 不重试，分类 AUTHENTICATION_ERROR。"""
        sleeps: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, content=_error_json("invalid key"))

        client = _make_client(handler, sleeps)
        with pytest.raises(ApiClientError) as excinfo:
            client.chat(_request())
        err: ApiClientError = excinfo.value
        assert err.record.error_category == ErrorCategory.AUTHENTICATION_ERROR
        assert len(err.record.attempts) == 1
        assert sleeps == []

    def test_retry_connect_error(self) -> None:
        """ConnectError 视为 NETWORK_ERROR 且可重试。"""
        sleeps: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            msg = "conn refused"
            raise httpx.ConnectError(msg, request=request)

        client = _make_client(handler, sleeps, max_retries=2)
        with pytest.raises(ApiClientError) as excinfo:
            client.chat(_request())
        err: ApiClientError = excinfo.value
        assert err.record.error_category == ErrorCategory.NETWORK_ERROR
        assert err.record.retry_count == 2
        assert err.record.attempts[0].may_be_billed is False
        assert len(sleeps) == 2

    def test_read_timeout_no_retry_by_default(self) -> None:
        """ReadTimeout 默认不重试（allow_retry_on_timeout=False），TIMEOUT_UNKNOWN。"""
        sleeps: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            msg = "read timeout"
            raise httpx.ReadTimeout(msg, request=request)

        client = _make_client(handler, sleeps, allow_retry_on_timeout=False)
        with pytest.raises(ApiClientError) as excinfo:
            client.chat(_request())
        err: ApiClientError = excinfo.value
        assert err.record.error_category == ErrorCategory.TIMEOUT_UNKNOWN
        assert err.record.retry_count == 0
        assert err.record.attempts[0].may_be_billed is True
        assert sleeps == []

    def test_read_timeout_retried_when_allowed(self) -> None:
        """allow_retry_on_timeout=True 时 ReadTimeout 重试。"""
        sleeps: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            msg = "read timeout"
            raise httpx.ReadTimeout(msg, request=request)

        client = _make_client(handler, sleeps, allow_retry_on_timeout=True, max_retries=2)
        with pytest.raises(ApiClientError) as excinfo:
            client.chat(_request())
        err: ApiClientError = excinfo.value
        assert err.record.error_category == ErrorCategory.TIMEOUT_UNKNOWN
        assert err.record.retry_count == 2
        assert len(sleeps) == 2

    def test_unknown_exception_no_retry(self) -> None:
        """未知异常 → UNKNOWN_ERROR 且不重试。"""
        sleeps: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            msg = "boom"
            raise ValueError(msg)

        client = _make_client(handler, sleeps)
        with pytest.raises(ApiClientError) as excinfo:
            client.chat(_request())
        err: ApiClientError = excinfo.value
        assert err.record.error_category == ErrorCategory.UNKNOWN_ERROR
        assert err.record.retry_count == 0
        assert sleeps == []

    def test_retry_budget_capped_at_max_retries(self) -> None:
        """重试次数上限：429 持续时最多 max_retries 次重试。"""
        sleeps: list[float] = []
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return httpx.Response(429, content=_error_json("nope"))

        client = _make_client(handler, sleeps, max_retries=1)
        with pytest.raises(ApiClientError):
            client.chat(_request())
        assert call_count["n"] == 2  # 1 + 1 次重试
        assert len(sleeps) == 1


# ------------------------------------------------------------------ 错误信息


class TestSafeError:
    """脱敏错误信息与原始引用。"""

    def test_safe_error_extracts_message_and_redacts(self) -> None:
        """safe_error_message 应提取 error.message 并脱敏。"""
        sleeps: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                429, content=_error_json("please wait, token=sk-supersecret12345")
            )

        client = _make_client(handler, sleeps)
        with pytest.raises(ApiClientError) as excinfo:
            client.chat(_request())
        err: ApiClientError = excinfo.value
        assert "sk-supersecret12345" not in (err.record.safe_error_message or "")
        assert "[REDACTED]" in (err.record.safe_error_message or "")
        assert err.record.raw_error_reference is not None
        assert err.record.raw_error_reference.startswith("sha256:")
        assert len(err.record.raw_error_reference) == len("sha256:") + 16


# ------------------------------------------------------------------ 日志


class TestLogging:
    """请求记录器调用。"""

    def test_logger_called_once_success_with_attempts(self) -> None:
        """成功时 logger 恰好调用一次，含完整 attempts。"""
        sleeps: list[float] = []
        counts = {"n": 0}
        logger = RequestLoggerSpy()

        def handler(request: httpx.Request) -> httpx.Response:
            counts["n"] += 1
            if counts["n"] == 1:
                return httpx.Response(
                    429, headers={"Retry-After": "0.2"}, content=_error_json("slow")
                )
            return httpx.Response(200, content=_ok_json())

        client = _make_client(handler, sleeps, logger=logger)
        client.chat(_request())
        assert len(logger.records) == 1
        assert len(logger.records[0].attempts) == 2

    def test_logger_called_once_on_failure(self) -> None:
        """失败抛出时 logger 恰好调用一次。"""
        sleeps: list[float] = []
        logger = RequestLoggerSpy()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, content=_error_json("bad"))

        client = _make_client(handler, sleeps, logger=logger)
        with pytest.raises(ApiClientError):
            client.chat(_request())
        assert len(logger.records) == 1


# ------------------------------------------------------------------ 成本上限


class TestCostLimit:
    """成本上限保护。"""

    def test_cost_limit_exceeded_raises_budget(self) -> None:
        """估算成本超限 → BUDGET_EXCEEDED。"""
        sleeps: list[float] = []
        price_table = {
            _DEFAULT_MODEL: {
                "input_per_million": 10.0,
                "output_per_million": 10.0,
                "currency": "CNY",
            }
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_ok_json())

        # 3*1e-6*10 + 2*1e-6*10 = 0.00005，设超低限额触发。
        client = _make_client(handler, sleeps, price_table=price_table)
        with pytest.raises(ApiClientError) as excinfo:
            client.chat(_request(cost_limit=0.000001))
        err: ApiClientError = excinfo.value
        assert err.record.error_category == ErrorCategory.BUDGET_EXCEEDED
        assert err.record.estimated_cost is not None
        assert err.record.estimated_cost == pytest.approx(0.00005)

    def test_cost_within_limit_succeeds(self) -> None:
        """估算成本在限内则成功返回。"""
        sleeps: list[float] = []
        price_table = {
            _DEFAULT_MODEL: {"input_per_million": 1.0, "output_per_million": 1.0, "currency": "CNY"}
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_ok_json())

        client = _make_client(handler, sleeps, price_table=price_table)
        resp = client.chat(_request(cost_limit=10.0))
        assert resp.record.error_category is None
        assert resp.record.cost_status == "estimated"


# ------------------------------------------------------------------ 流式


_STREAM_BODY = (
    'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
    'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
    'data: {"choices":[{"delta":{"content":""}}],'
    '"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}\n\n'
    "data: [DONE]\n\n"
)


class TestStream:
    """流式响应。"""

    def test_stream_success_yields_deltas_and_usage(self) -> None:
        """流式成功：产出增量并记录最终用量。"""
        sleeps: list[float] = []
        logger = RequestLoggerSpy()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_STREAM_BODY)

        client = _make_client(handler, sleeps, logger=logger)
        chunks = list(client.chat_stream(_request(stream=True)))

        assert [c.delta for c in chunks] == ["Hel", "lo"]
        assert len(logger.records) == 1
        record = logger.records[0]
        assert record.streaming is True
        assert record.status_code == 200
        assert record.input_tokens == 3
        assert record.output_tokens == 2
        assert record.error_category is None

    def test_stream_abandoned_records_cancelled(self) -> None:
        """消费者放弃 → 记录 CANCELLED 并关闭。"""
        sleeps: list[float] = []
        logger = RequestLoggerSpy()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_STREAM_BODY)

        client = _make_client(handler, sleeps, logger=logger)
        gen = cast(
            "Generator[StreamChunk, None, None]", client.chat_stream(_request(stream=True))
        )
        first = next(gen)
        gen.close()

        assert first.delta == "Hel"
        assert len(logger.records) == 1
        assert logger.records[0].error_category == ErrorCategory.CANCELLED
        assert logger.records[0].streaming is True
        assert logger.records[0].attempts[0].may_be_billed is True

    def test_stream_read_failure_records_network_error(self) -> None:
        """流中途 httpx 错误 → NETWORK_ERROR。"""
        sleeps: list[float] = []
        logger = RequestLoggerSpy()

        def handler(request: httpx.Request) -> httpx.Response:
            msg = "dropped"
            raise httpx.ConnectError(msg, request=request)

        client = _make_client(handler, sleeps, logger=logger)
        with pytest.raises(ApiClientError) as excinfo:
            list(client.chat_stream(_request(stream=True)))
        err: ApiClientError = excinfo.value
        assert err.record.error_category == ErrorCategory.NETWORK_ERROR
        assert err.record.streaming is True
        assert len(logger.records) == 1
