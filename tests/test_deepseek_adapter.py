"""DeepSeek Provider Adapter 测试（S1-03）。

覆盖：请求体构造、状态码→错误分类全映射、异常分类、Usage 提取、
``Retry-After`` 多格式、成本估算（有/无价格表）、模型能力映射。
"""

from __future__ import annotations

import email.utils
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import pytest

from nexusai_lite.providers.deepseek import DeepSeekAdapter
from nexusai_lite.records import ApiRequest, ErrorCategory, Usage

if TYPE_CHECKING:
    from collections.abc import Mapping


def _adapter(
    base_url: str = "https://api.deepseek.com",
    price_table: Mapping[str, Mapping[str, float | str]] | None = None,
) -> DeepSeekAdapter:
    """构造适配器；默认无价格表（成本未知）。"""
    return DeepSeekAdapter(base_url=base_url, price_table=price_table)


def _request(
    *,
    stream: bool = False,
    max_tokens: int | None = None,
    temperature: float | None = None,
    extra_body: dict[str, object] | None = None,
) -> ApiRequest:
    return ApiRequest(
        model="deepseek-chat",
        messages=[{"role": "user", "content": "hi"}],
        stream=stream,
        max_tokens=max_tokens,
        temperature=temperature,
        extra_body=dict(extra_body) if extra_body is not None else {},
    )


# ------------------------------------------------------------------ build_request


class TestBuildRequest:
    """build_request 构造请求体。"""

    def test_basic_non_stream(self) -> None:
        """非流式：URL、model、messages 正确，且不含认证与 None 字段。"""
        adapter = _adapter()
        req = _request()
        built = adapter.build_request(req)
        assert built.url == "https://api.deepseek.com/chat/completions"
        assert built.json_body["model"] == "deepseek-chat"
        assert built.json_body["messages"] == [{"role": "user", "content": "hi"}]
        assert "stream" not in built.json_body
        assert "stream_options" not in built.json_body
        assert "max_tokens" not in built.json_body
        assert "temperature" not in built.json_body
        assert "Authorization" not in built.json_body

    def test_stream_includes_options(self) -> None:
        """流式请求应追加 stream_options.include_usage。"""
        adapter = _adapter()
        built = adapter.build_request(_request(stream=True))
        assert built.json_body["stream"] is True
        assert built.json_body["stream_options"] == {"include_usage": True}

    def test_optional_fields_present_when_set(self) -> None:
        """max_tokens/temperature 设置时才写入。"""
        adapter = _adapter()
        built = adapter.build_request(
            _request(max_tokens=512, temperature=0.7, extra_body={"top_p": 0.9})
        )
        assert built.json_body["max_tokens"] == 512
        assert built.json_body["temperature"] == 0.7
        assert built.json_body["top_p"] == 0.9

    def test_extra_body_merged_and_overrides(self) -> None:
        """extra_body 合并，且可选字段为 None 时不写入。"""
        adapter = _adapter()
        built = adapter.build_request(_request(temperature=None, extra_body={"temperature": 1.1}))
        # extra_body 覆盖同名字段
        assert built.json_body["temperature"] == 1.1

    def test_custom_base_url(self) -> None:
        """自定义 base_url 应生效且去除尾部斜杠。"""
        adapter = _adapter(base_url="https://example.com/api/")
        built = adapter.build_request(_request())
        assert built.url == "https://example.com/api/chat/completions"

    def test_name_is_deepseek(self) -> None:
        """供应商标识名应为 deepseek。"""
        assert _adapter().name == "deepseek"


# ------------------------------------------------------------------ classify_error


class TestClassifyStatus:
    """状态码→错误分类全映射。"""

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (400, ErrorCategory.INVALID_REQUEST),
            (401, ErrorCategory.AUTHENTICATION_ERROR),
            (402, ErrorCategory.BUDGET_EXCEEDED),
            (403, ErrorCategory.PERMISSION_ERROR),
            (422, ErrorCategory.INVALID_REQUEST),
            (429, ErrorCategory.RATE_LIMITED),
            (500, ErrorCategory.PROVIDER_SERVER_ERROR),
            (503, ErrorCategory.PROVIDER_OVERLOADED),
            (504, ErrorCategory.PROVIDER_SERVER_ERROR),
            (418, ErrorCategory.INVALID_REQUEST),
            (200, ErrorCategory.UNKNOWN_ERROR),
        ],
    )
    def test_status_matrix(self, status: int, expected: ErrorCategory) -> None:
        """各状态码应映射到预期分类。"""
        adapter = _adapter()
        assert (
            adapter.classify_error(status_code=status, response_body=None, exc=None)
            == expected
        )

    def test_other_5xx_server_error(self) -> None:
        """其他 5xx 视为 PROVIDER_SERVER_ERROR。"""
        adapter = _adapter()
        assert (
            adapter.classify_error(status_code=502, response_body=None, exc=None)
            == ErrorCategory.PROVIDER_SERVER_ERROR
        )

    def test_other_4xx_invalid_request(self) -> None:
        """其他 4xx 视为 INVALID_REQUEST。"""
        adapter = _adapter()
        assert (
            adapter.classify_error(status_code=409, response_body=None, exc=None)
            == ErrorCategory.INVALID_REQUEST
        )


class TestClassifyException:
    """无状态码时的异常分类。"""

    def _classify(self, exc: BaseException) -> ErrorCategory:
        return _adapter().classify_error(status_code=None, response_body=None, exc=exc)

    def test_connect_timeout_network(self) -> None:
        """ConnectTimeout 应视为 NETWORK_ERROR。"""
        exc = httpx.ConnectTimeout("connect", request=httpx.Request("POST", "http://x"))
        assert self._classify(exc) == ErrorCategory.NETWORK_ERROR

    def test_connect_error_network(self) -> None:
        """ConnectError 应视为 NETWORK_ERROR。"""
        exc = httpx.ConnectError("conn", request=httpx.Request("POST", "http://x"))
        assert self._classify(exc) == ErrorCategory.NETWORK_ERROR

    @pytest.mark.parametrize(
        "exc_cls",
        [httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout],
    )
    def test_read_write_pool_timeout_unknown(
        self, exc_cls: type[httpx.TimeoutException]
    ) -> None:
        """Read/Write/Pool 超时应视为 TIMEOUT_UNKNOWN。"""
        request = httpx.Request("POST", "http://x")
        # PoolTimeout 构造签名与 ReadTimeout 一致。
        exc = exc_cls("timeout", request=request)
        assert self._classify(exc) == ErrorCategory.TIMEOUT_UNKNOWN

    def test_other_request_error_network(self) -> None:
        """其他 httpx.RequestError 应视为 NETWORK_ERROR。"""
        exc = httpx.NetworkError("boom", request=httpx.Request("POST", "http://x"))
        assert self._classify(exc) == ErrorCategory.NETWORK_ERROR

    def test_unknown_exception(self) -> None:
        """非 httpx 异常应视为 UNKNOWN_ERROR。"""
        assert self._classify(ValueError("boom")) == ErrorCategory.UNKNOWN_ERROR


# ------------------------------------------------------------------ extract_usage


class TestExtractUsage:
    """从响应 JSON 提取 Token 用量。"""

    def test_valid_usage(self) -> None:
        """合法用法应提取完整 token 字段。"""
        adapter = _adapter()
        usage = adapter.extract_usage(
            {"usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
        )
        assert usage == Usage(input_tokens=10, output_tokens=5, total_tokens=15)

    def test_missing_usage(self) -> None:
        """缺失 usage 返回 None。"""
        adapter = _adapter()
        assert adapter.extract_usage({}) is None

    def test_invalid_tokens(self) -> None:
        """token 缺失/非法返回 None。"""
        adapter = _adapter()
        assert adapter.extract_usage({"usage": {"prompt_tokens": "x"}}) is None
        assert adapter.extract_usage({"usage": {"completion_tokens": 5}}) is None


# ------------------------------------------------------------------ extract_retry_after


class TestExtractRetryAfter:
    """Retry-After 多格式解析。"""

    def test_integer_seconds(self) -> None:
        """整数秒。"""
        assert _adapter().extract_retry_after({"Retry-After": "12"}) == 12.0

    def test_float_seconds(self) -> None:
        """浮点秒。"""
        assert _adapter().extract_retry_after({"Retry-After": "0.5"}) == 0.5

    def test_lowercase_header_key(self) -> None:
        """小写 header 键也应解析。"""
        assert _adapter().extract_retry_after({"retry-after": "3"}) == 3.0

    def test_http_date(self) -> None:
        """HTTP-date 格式应解析为距现时的秒数（≥0）。"""
        future = datetime.now(UTC) + timedelta(seconds=30)
        value = email.utils.format_datetime(future)
        secs = _adapter().extract_retry_after({"Retry-After": value})
        assert secs is not None
        assert 25 <= secs <= 35

    def test_absent(self) -> None:
        """缺失返回 None。"""
        assert _adapter().extract_retry_after({}) is None

    def test_invalid(self) -> None:
        """非法值返回 None。"""
        assert _adapter().extract_retry_after({"Retry-After": "abc"}) is None
        assert _adapter().extract_retry_after({"Retry-After": ""}) is None


# ------------------------------------------------------------------ estimate_cost


class TestEstimateCost:
    """成本估算：有/无价格表。"""

    def test_without_table_unknown(self) -> None:
        """无价格表时返回 unknown。"""
        adapter = _adapter()
        est = adapter.estimate_cost(
            Usage(input_tokens=1000, output_tokens=500),
            "deepseek-chat",
            pricing_version="unverified-seed",
        )
        assert est.estimated_cost is None
        assert est.cost_status == "unknown"

    def test_with_table_estimated(self) -> None:
        """有价格表时按每百万 token 估算。"""
        adapter = _adapter(
            price_table={
                "deepseek-chat": {
                    "input_per_million": 1.0,
                    "output_per_million": 2.0,
                    "currency": "CNY",
                }
            },
        )
        est = adapter.estimate_cost(
            Usage(input_tokens=1_000_000, output_tokens=500_000), "deepseek-chat"
        )
        assert est.estimated_cost == pytest.approx(1.0 + 1.0)  # 1*1 + 0.5*2
        assert est.cost_status == "estimated"

    def test_with_table_missing_model(self) -> None:
        """价格表中无该模型时返回 unknown。"""
        adapter = _adapter(price_table={"other": {"input_per_million": 1, "output_per_million": 1}})
        est = adapter.estimate_cost(Usage(input_tokens=1, output_tokens=1), "deepseek-chat")
        assert est.estimated_cost is None
        assert est.cost_status == "unknown"


# ------------------------------------------------------------------ get_capabilities


class TestGetCapabilities:
    """模型能力映射（保守）。"""

    def test_capabilities(self) -> None:
        """应返回保守且不臆造限制的能力映射。"""
        caps = _adapter().get_capabilities("deepseek-chat")
        assert caps == {
            "model": "deepseek-chat",
            "supports_streaming": True,
            "supports_usage": True,
        }
