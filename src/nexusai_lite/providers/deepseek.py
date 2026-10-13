"""DeepSeek Provider Adapter（S1-03）。

封装 DeepSeek Chat Completions API 的请求格式、错误分类、``Retry-After``、
Usage 提取与成本估算。价格表默认空（成本未知），不臆造官方价格。
"""

# ruff: noqa: ARG002, C901, PLR0911, PLR2004
# ARG002: 接口强制的 response_body 参数（与 ProviderAdapter 契约一致），DeepSeek 分类不使用。
# C901/PLR0911/PLR2004: 状态码→错误分类为需求规定的查表映射，逐码常量与分支是刻意为之。

from __future__ import annotations

import email.utils
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx

from nexusai_lite.providers.base import AdapterRequest, ProviderAdapter
from nexusai_lite.records import (
    ApiRequest,
    CostEstimate,
    ErrorCategory,
    Usage,
)

_DEFAULT_BASE_URL = "https://api.deepseek.com"

_CostTableRow = Mapping[str, float | str]  # {input_per_million, output_per_million, currency}


class DeepSeekAdapter(ProviderAdapter):
    """DeepSeek 官方 Chat Completions API 的适配器。"""

    def __init__(
        self,
        base_url: str = _DEFAULT_BASE_URL,
        price_table: Mapping[str, _CostTableRow] | None = None,
    ) -> None:
        """初始化适配器。

        Args:
            base_url: DeepSeek API 根地址（不含 ``/chat/completions``）。
            price_table: 模型名 → 每百万 token 价格行
                ``{"input_per_million", "output_per_million", "currency"}``。
                默认空表 → 成本估算返回 unknown，不臆造价格。
        """
        self._base_url = base_url.rstrip("/")
        self._price_table: Mapping[str, _CostTableRow] = (
            dict(price_table) if price_table is not None else {}
        )

    @property
    def name(self) -> str:
        """供应商标识名。"""
        return "deepseek"

    def build_request(self, request: ApiRequest) -> AdapterRequest:
        """构造 POST ``{base_url}/chat/completions`` 的请求载体。

        不在此处放入 API Key（由 API Client 统一添加认证头）。流式请求
        追加 ``stream_options.include_usage`` 以便从最终块获取 Token 用量。
        """
        body: dict[str, Any] = {"model": request.model, "messages": request.messages}
        if request.stream:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
        if request.max_tokens is not None:
            body["max_tokens"] = request.max_tokens
        if request.temperature is not None:
            body["temperature"] = request.temperature
        body.update(request.extra_body)
        return AdapterRequest(url=f"{self._base_url}/chat/completions", json_body=body)

    def classify_error(
        self,
        *,
        status_code: int | None,
        response_body: str | None,
        exc: BaseException | None,
    ) -> ErrorCategory:
        """把 DeepSeek API 响应/异常映射为统一错误分类。

        优先级：有状态码时按状态码分类；无状态码时按异常类型分类；
        都无法识别则归为 UNKNOWN_ERROR（保守处理，不因无法识别而无限重试）。
        """
        if status_code is not None:
            return self._classify_status(status_code)
        if exc is not None:
            return self._classify_exception(exc)
        return ErrorCategory.UNKNOWN_ERROR

    @staticmethod
    def _classify_status(status_code: int) -> ErrorCategory:
        """按 HTTP 状态码分类。"""
        if status_code == 400:
            return ErrorCategory.INVALID_REQUEST
        if status_code == 401:
            return ErrorCategory.AUTHENTICATION_ERROR
        if status_code == 402:
            # 402 Payment Required：余额不足（DeepSeek 用 402 表示余额不足）。
            return ErrorCategory.BUDGET_EXCEEDED
        if status_code == 403:
            return ErrorCategory.PERMISSION_ERROR
        if status_code == 422:
            return ErrorCategory.INVALID_REQUEST
        if status_code == 429:
            return ErrorCategory.RATE_LIMITED
        if status_code == 500:
            return ErrorCategory.PROVIDER_SERVER_ERROR
        if status_code == 503:
            return ErrorCategory.PROVIDER_OVERLOADED
        if 500 <= status_code <= 599:
            return ErrorCategory.PROVIDER_SERVER_ERROR
        if 400 <= status_code <= 499:
            return ErrorCategory.INVALID_REQUEST
        return ErrorCategory.UNKNOWN_ERROR

    @staticmethod
    def _classify_exception(exc: BaseException) -> ErrorCategory:
        """按异常类型分类（无状态码时）。"""
        if isinstance(exc, httpx.ConnectTimeout | httpx.ConnectError):
            # 连接失败：请求可能未到达服务器，不会计费。
            return ErrorCategory.NETWORK_ERROR
        if isinstance(exc, httpx.ReadTimeout | httpx.WriteTimeout | httpx.PoolTimeout):
            # 读写超时：请求可能已发出/执行，是否计费不确定。
            return ErrorCategory.TIMEOUT_UNKNOWN
        if isinstance(exc, httpx.RequestError):
            # 其余 httpx 传输错误视为网络错误。
            return ErrorCategory.NETWORK_ERROR
        return ErrorCategory.UNKNOWN_ERROR

    def extract_usage(self, response_json: Mapping[str, Any]) -> Usage | None:
        """从响应 JSON 提取 Token 用量；缺失或非法返回 None。"""
        usage = response_json.get("usage")
        if not isinstance(usage, Mapping):
            return None
        try:
            input_tokens = int(usage["prompt_tokens"])
            output_tokens = int(usage["completion_tokens"])
        except (KeyError, TypeError, ValueError):
            return None
        raw_total = usage.get("total_tokens")
        total_tokens = int(raw_total) if isinstance(raw_total, (int, float)) else None
        return Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )

    def extract_retry_after(self, headers: Mapping[str, str]) -> float | None:
        """提取 ``Retry-After``（秒数或 HTTP-date）；无效/缺失返回 None。"""
        value = headers.get("Retry-After") or headers.get("retry-after")
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            pass
        # 尝试 HTTP-date 格式（RFC 7231）。
        try:
            parsed = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if parsed is None:
            return None
        delta = (parsed - datetime.now(UTC)).total_seconds()
        return max(delta, 0.0)

    def estimate_cost(
        self,
        usage: Usage,
        model: str,
        pricing_version: str | None = None,
    ) -> CostEstimate:
        """按价格表估算成本；无价格条目时返回 unknown。

        公式：``input/1e6*in_price + output/1e6*out_price``。
        """
        row = self._price_table.get(model)
        if row is None:
            return CostEstimate(
                estimated_cost=None,
                cost_status="unknown",
                pricing_version=pricing_version,
            )
        try:
            in_price = float(row["input_per_million"])
            out_price = float(row["output_per_million"])
        except (KeyError, TypeError, ValueError):
            return CostEstimate(
                estimated_cost=None,
                cost_status="unknown",
                pricing_version=pricing_version,
            )
        estimated_cost = usage.input_tokens / 1e6 * in_price + usage.output_tokens / 1e6 * out_price
        return CostEstimate(
            estimated_cost=estimated_cost,
            cost_status="estimated",
            pricing_version=pricing_version,
        )

    def get_capabilities(self, model: str) -> Mapping[str, Any]:
        """返回模型能力（保守映射，不臆造限制）。"""
        return {
            "model": model,
            "supports_streaming": True,
            "supports_usage": True,
        }
