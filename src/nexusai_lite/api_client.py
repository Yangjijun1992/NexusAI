"""统一 API Client（S1-03）。

业务模块不得各自直接调用供应商 SDK（reference §5.2 / §12 规则 1）；所有模型
请求都经此入口。负责：构造请求、有限重试（尊重 ``Retry-After`` 与指数退避）、
错误分类、Token 用量解析、成本估算、流式/非流式处理，以及请求记录（脱敏）。
"""

# ruff: noqa: C901, PLR0913, PLR0915, PLR0917, PLR2004, S311, TRY301
# C901/PLR0915: chat/chat_stream 为 S1-03 需求规定的复杂重试与 SSE 管线，刻意为之。
# PLR0913/PLR0917: 构造器 12 参数为冻结接口；内部 builder 字段与冻结记录一一对应。
# PLR2004: HTTP 状态码为惯用常量，非魔法数字。
# S311: 退避抖动使用非加密随机，用于调度而非安全。
# TRY301: GeneratorExit 分支的裸 raise 用于正确重抛放弃信号。

from __future__ import annotations

import hashlib
import json
import random
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from nexusai_lite.db import utcnow_iso
from nexusai_lite.logging_setup import redact_text
from nexusai_lite.records import (
    RETRYABLE_CATEGORIES,
    ApiRequest,
    AttemptRecord,
    CostEstimate,
    ErrorCategory,
    RequestLogger,
    RequestRecord,
    Usage,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from nexusai_lite.providers.base import ProviderAdapter

_MAX_SAFE_MESSAGE_CHARS = 200


class ApiClientError(Exception):
    """重试耗尽或不可重试错误时抛出，携带最终 :class:`RequestRecord`。"""

    def __init__(self, record: RequestRecord) -> None:
        """保存最终记录并抛出错误。"""
        self.record = record
        super().__init__(f"API 请求失败: {record.error_category}")


@dataclass(frozen=True)
class StreamChunk:
    """流式响应的一个增量块。"""

    delta: str
    done: bool = False


@dataclass(frozen=True)
class ApiResponse:
    """非流式响应结果。"""

    request_id: str
    model: str
    status_code: int
    content: str | None
    usage: Usage | None
    record: RequestRecord


class ApiClient:
    """统一 API Client：封装请求、有限重试、成本上限与请求记录。

    Args:
        adapter: 目标 Provider 的适配器。
        api_key: API Key（若设置则以 ``Authorization: Bearer`` 发送）。
        timeout_seconds: 默认超时（创建内部客户端时使用）。
        max_retries: 默认最大重试次数（请求可覆盖）。
        backoff_base_seconds: 指数退避基数。
        backoff_max_seconds: 指数退避上限。
        retry_jitter: 退避抖动比例（0..1）。
        allow_retry_on_timeout: 超时是否默认重试（请求可覆盖）。
        logger: 请求记录器；为 None 时跳过记录。
        http_client: 注入的 httpx 客户端（永不关闭）；为 None 时内部创建并复用。
        sleep_fn: 退避等待函数（测试可注入 no-op）。
    """

    def __init__(
        self,
        adapter: ProviderAdapter,
        *,
        api_key: str | None = None,
        timeout_seconds: float = 60.0,
        max_retries: int = 2,
        backoff_base_seconds: float = 1.0,
        backoff_max_seconds: float = 30.0,
        retry_jitter: float = 0.25,
        allow_retry_on_timeout: bool = False,
        logger: RequestLogger | None = None,
        http_client: httpx.Client | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        """构造统一 API Client，细节见类 docstring。"""
        self._adapter = adapter
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._backoff_base = backoff_base_seconds
        self._backoff_max = backoff_max_seconds
        self._retry_jitter = retry_jitter
        self._allow_retry_on_timeout = allow_retry_on_timeout
        self._logger = logger
        # 注入客户端不关闭（调用方拥有所有权）；内部创建则复用单例。
        self._client = http_client or httpx.Client(timeout=timeout_seconds)
        self._sleep_fn = sleep_fn

    # ------------------------------------------------------------------ 非流式

    def chat(self, request: ApiRequest) -> ApiResponse:
        """执行一次非流式请求，含有限重试；耗尽重试或不可重试时抛错。"""
        request_id = self._new_request_id()
        started_iso = utcnow_iso()
        started_mono = time.monotonic()
        adapter_request = self._adapter.build_request(request)
        headers = self._build_headers(request_id)
        max_attempts = self._resolved_max_retries(request) + 1
        allow_retry_timeout = self._resolved_allow_timeout(request)
        attempts: list[AttemptRecord] = []

        for attempt_no in range(1, max_attempts + 1):
            a_started_iso = utcnow_iso()
            a_started_mono = time.monotonic()
            try:
                resp = self._client.post(
                    adapter_request.url,
                    json=adapter_request.json_body,
                    headers=headers,
                )
            except httpx.RequestError as exc:
                error_category = self._adapter.classify_error(
                    status_code=None, response_body=None, exc=exc
                )
                attempts.append(
                    self._attempt_for_exception(attempt_no, a_started_iso, a_started_mono, exc)
                )
                if self._can_retry(
                    error_category,
                    attempt_no,
                    max_attempts,
                    allow_retry_timeout=allow_retry_timeout,
                ):
                    self._sleep_fn(self._compute_delay(error_category, attempt_no, None))
                    continue
                record = self._make_record(
                    request,
                    request_id,
                    started_iso,
                    a_started_iso,
                    started_mono,
                    a_started_mono,
                    status_code=None,
                    error_category=error_category,
                    safe_error=self._safe_message(None, None, exc),
                    raw_ref=None,
                    input_tokens=None,
                    output_tokens=None,
                    estimate=None,
                    streaming=False,
                    attempts=attempts,
                )
                self._log(record)
                raise ApiClientError(record) from None
            except Exception as exc:  # noqa: BLE001 - 未知异常保守归类，不重试
                error_category = ErrorCategory.UNKNOWN_ERROR
                attempts.append(
                    AttemptRecord(
                        attempt_no=attempt_no,
                        started_at=a_started_iso,
                        ended_at=utcnow_iso(),
                        status_code=None,
                        error_category=error_category,
                        retry_reason="unexpected_exception",
                        may_be_billed=True,
                        latency_ms=int((time.monotonic() - a_started_mono) * 1000),
                    )
                )
                record = self._make_record(
                    request,
                    request_id,
                    started_iso,
                    a_started_iso,
                    started_mono,
                    a_started_mono,
                    status_code=None,
                    error_category=error_category,
                    safe_error=self._safe_message(None, None, exc),
                    raw_ref=None,
                    input_tokens=None,
                    output_tokens=None,
                    estimate=None,
                    streaming=False,
                    attempts=attempts,
                )
                self._log(record)
                raise ApiClientError(record) from None

            status_code = resp.status_code
            body = self._read_body(resp)
            error_category = self._adapter.classify_error(
                status_code=status_code, response_body=body, exc=None
            )
            attempt = self._make_attempt(
                attempt_no,
                a_started_iso,
                a_started_mono,
                status_code=status_code,
                error_category=error_category,
                retry_reason=None,
                may_be_billed=True,
            )
            attempts.append(attempt)

            if 200 <= status_code < 300:
                parsed = self._parse_json(body)
                response_json = parsed if isinstance(parsed, Mapping) else None
                usage = self._adapter.extract_usage(response_json) if response_json else None
                estimate = self._adapter.estimate_cost(usage, request.model) if usage else None
                input_tokens = usage.input_tokens if usage else None
                output_tokens = usage.output_tokens if usage else None
                if (
                    request.cost_limit is not None
                    and estimate is not None
                    and estimate.estimated_cost is not None
                    and estimate.estimated_cost > request.cost_limit
                ):
                    record = self._make_record(
                        request,
                        request_id,
                        started_iso,
                        a_started_iso,
                        started_mono,
                        a_started_mono,
                        status_code=status_code,
                        error_category=ErrorCategory.BUDGET_EXCEEDED,
                        safe_error=(
                            f"成本估算 {estimate.estimated_cost} 超过上限 {request.cost_limit}"
                        ),
                        raw_ref=None,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        estimate=estimate,
                        streaming=False,
                        attempts=attempts,
                    )
                    self._log(record)
                    raise ApiClientError(record)
                record = self._make_record(
                    request,
                    request_id,
                    started_iso,
                    a_started_iso,
                    started_mono,
                    a_started_mono,
                    status_code=status_code,
                    error_category=None,
                    safe_error=None,
                    raw_ref=None,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    estimate=estimate,
                    streaming=False,
                    attempts=attempts,
                )
                self._log(record)
                return ApiResponse(
                    request_id=request_id,
                    model=request.model,
                    status_code=status_code,
                    content=body,
                    usage=usage,
                    record=record,
                )

            # 错误响应：可能可重试，否则终止。
            if self._can_retry(
                error_category, attempt_no, max_attempts, allow_retry_timeout=allow_retry_timeout
            ):
                retry_after = self._retry_after_from_response(resp, error_category)
                self._sleep_fn(self._compute_delay(error_category, attempt_no, retry_after))
                continue
            record = self._make_record(
                request,
                request_id,
                started_iso,
                a_started_iso,
                started_mono,
                a_started_mono,
                status_code=status_code,
                error_category=error_category,
                safe_error=self._safe_message(status_code, body, None),
                raw_ref=self._raw_reference(body),
                input_tokens=None,
                output_tokens=None,
                estimate=None,
                streaming=False,
                attempts=attempts,
            )
            self._log(record)
            raise ApiClientError(record)

        # 防御：正常路径在循环内 return/raise；仅当 max_attempts<1 时才可达。
        unreachable_msg = "chat loop exhausted without result"
        raise AssertionError(unreachable_msg)

    # ------------------------------------------------------------------ 流式

    def chat_stream(self, request: ApiRequest) -> Iterator[StreamChunk]:
        """执行流式请求，产出增量块；结束时记录用量与状态。"""
        request_id = self._new_request_id()
        started_iso = utcnow_iso()
        started_mono = time.monotonic()
        adapter_request = self._adapter.build_request(request)
        headers = self._build_headers(request_id)
        status_code: int | None = None
        usage: Usage | None = None
        a_started_iso = started_iso
        a_started_mono = started_mono
        attempts: list[AttemptRecord] = []

        try:
            with self._client.stream(
                "POST",
                adapter_request.url,
                json=adapter_request.json_body,
                headers=headers,
            ) as resp:
                status_code = resp.status_code
                if resp.status_code >= 400:
                    body = self._read_body(resp)
                    error_category = self._classify_response_error(resp.status_code, body)
                    attempts.append(
                        self._stream_attempt(
                            a_started_iso,
                            a_started_mono,
                            status_code,
                            error_category,
                            "stream_error_response",
                            may_be_billed=True,
                        )
                    )
                    record = self._stream_record(
                        request,
                        request_id,
                        started_iso,
                        started_mono,
                        a_started_iso,
                        a_started_mono,
                        status_code,
                        error_category,
                        safe_error=self._safe_message(resp.status_code, body, None),
                        raw_ref=self._raw_reference(body),
                        usage=None,
                        attempts=attempts,
                    )
                    self._log(record)
                    raise ApiClientError(record)

                for raw_line in resp.iter_lines():
                    if not raw_line:
                        continue
                    line = raw_line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if data == "[DONE]":
                        break
                    payload = self._parse_json(data)
                    if isinstance(payload, Mapping) and "usage" in payload:
                        stream_usage = self._adapter.extract_usage(payload)
                        if stream_usage is not None:
                            usage = stream_usage
                    delta = self._extract_delta(payload) if isinstance(payload, Mapping) else ""
                    if delta:
                        yield StreamChunk(delta=delta)

                attempts.append(
                    self._stream_attempt(
                        a_started_iso,
                        a_started_mono,
                        status_code,
                        None,
                        None,
                        may_be_billed=True,
                    )
                )
                record = self._stream_record(
                    request,
                    request_id,
                    started_iso,
                    started_mono,
                    a_started_iso,
                    a_started_mono,
                    status_code,
                    None,
                    safe_error=None,
                    raw_ref=None,
                    usage=usage,
                    attempts=attempts,
                )
                self._log(record)

        except GeneratorExit:
            # 消费者放弃生成器：标记 CANCELLED；上游可能已计费，保守记 may_be_billed=True。
            attempts.append(
                self._stream_attempt(
                    a_started_iso,
                    a_started_mono,
                    status_code,
                    ErrorCategory.CANCELLED,
                    "stream_abandoned",
                    may_be_billed=True,
                )
            )
            record = self._stream_record(
                request,
                request_id,
                started_iso,
                started_mono,
                a_started_iso,
                a_started_mono,
                status_code,
                ErrorCategory.CANCELLED,
                safe_error=None,
                raw_ref=None,
                usage=usage,
                attempts=attempts,
            )
            self._log(record)
            raise
        except ApiClientError:
            raise
        except httpx.RequestError as exc:
            # 流中途读取失败：保守记为 NETWORK_ERROR 并保留已捕获的部分用量。
            attempts.append(
                self._stream_attempt(
                    a_started_iso,
                    a_started_mono,
                    status_code,
                    ErrorCategory.NETWORK_ERROR,
                    "stream_read_failure",
                    may_be_billed=True,
                )
            )
            record = self._stream_record(
                request,
                request_id,
                started_iso,
                started_mono,
                a_started_iso,
                a_started_mono,
                status_code,
                ErrorCategory.NETWORK_ERROR,
                safe_error=self._safe_message(status_code, None, exc),
                raw_ref=None,
                usage=usage,
                attempts=attempts,
            )
            self._log(record)
            raise ApiClientError(record) from None

    # ------------------------------------------------------------------ 内部辅助

    def _make_attempt(
        self,
        attempt_no: int,
        started_iso: str,
        started_mono: float,
        status_code: int | None,
        error_category: ErrorCategory | None,
        retry_reason: str | None,
        *,
        may_be_billed: bool | None,
    ) -> AttemptRecord:
        ended_mono = time.monotonic()
        return AttemptRecord(
            attempt_no=attempt_no,
            started_at=started_iso,
            ended_at=utcnow_iso(),
            status_code=status_code,
            error_category=error_category,
            retry_reason=retry_reason,
            may_be_billed=bool(may_be_billed),
            latency_ms=int((ended_mono - started_mono) * 1000),
        )

    def _attempt_for_exception(
        self,
        attempt_no: int,
        started_iso: str,
        started_mono: float,
        exc: httpx.RequestError,
    ) -> AttemptRecord:
        """按异常构造尝试记录：分类为超时/网络并确定是否可能计费。"""
        category = self._adapter.classify_error(status_code=None, response_body=None, exc=exc)
        may_be_billed = isinstance(exc, httpx.ReadTimeout | httpx.WriteTimeout | httpx.PoolTimeout)
        return AttemptRecord(
            attempt_no=attempt_no,
            started_at=started_iso,
            ended_at=utcnow_iso(),
            status_code=None,
            error_category=category,
            retry_reason="transport_error",
            may_be_billed=may_be_billed,
            latency_ms=int((time.monotonic() - started_mono) * 1000),
        )

    def _stream_attempt(
        self,
        started_iso: str,
        started_mono: float,
        status_code: int | None,
        error_category: ErrorCategory | None,
        retry_reason: str | None,
        *,
        may_be_billed: bool,
    ) -> AttemptRecord:
        return AttemptRecord(
            attempt_no=1,
            started_at=started_iso,
            ended_at=utcnow_iso(),
            status_code=status_code,
            error_category=error_category,
            retry_reason=retry_reason,
            may_be_billed=may_be_billed,
            latency_ms=int((time.monotonic() - started_mono) * 1000),
        )

    def _make_record(
        self,
        request: ApiRequest,
        request_id: str,
        started_iso: str,
        ended_iso: str,
        started_mono: float,
        ended_mono: float,
        status_code: int | None,
        error_category: ErrorCategory | None,
        safe_error: str | None,
        raw_ref: str | None,
        input_tokens: int | None,
        output_tokens: int | None,
        estimate: CostEstimate | None,
        *,
        streaming: bool,
        attempts: list[AttemptRecord],
    ) -> RequestRecord:
        return RequestRecord(
            request_id=request_id,
            provider=self._adapter.name,
            model=request.model,
            started_at=started_iso,
            ended_at=ended_iso,
            latency_ms=int((ended_mono - started_mono) * 1000),
            status_code=status_code,
            retry_count=len(attempts) - 1,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost=estimate.estimated_cost if estimate else None,
            cost_status=estimate.cost_status if estimate else "unknown",
            error_category=error_category,
            safe_error_message=safe_error,
            raw_error_reference=raw_ref,
            streaming=streaming,
            task_id=request.task_id,
            attempts=tuple(attempts),
        )

    def _stream_record(
        self,
        request: ApiRequest,
        request_id: str,
        started_iso: str,
        started_mono: float,
        ended_iso: str,
        ended_mono: float,
        status_code: int | None,
        error_category: ErrorCategory | None,
        safe_error: str | None,
        raw_ref: str | None,
        usage: Usage | None,
        attempts: list[AttemptRecord],
    ) -> RequestRecord:
        estimate = self._adapter.estimate_cost(usage, request.model) if usage else None
        return RequestRecord(
            request_id=request_id,
            provider=self._adapter.name,
            model=request.model,
            started_at=started_iso,
            ended_at=ended_iso,
            latency_ms=int((ended_mono - started_mono) * 1000),
            status_code=status_code,
            retry_count=0,
            input_tokens=usage.input_tokens if usage else None,
            output_tokens=usage.output_tokens if usage else None,
            estimated_cost=estimate.estimated_cost if estimate else None,
            cost_status=estimate.cost_status if estimate else "unknown",
            error_category=error_category,
            safe_error_message=safe_error,
            raw_error_reference=raw_ref,
            streaming=True,
            task_id=request.task_id,
            attempts=tuple(attempts),
        )

    def _build_headers(self, request_id: str) -> dict[str, str]:
        headers = {"X-Request-Id": request_id}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _resolved_max_retries(self, request: ApiRequest) -> int:
        return request.max_retries if request.max_retries is not None else self._max_retries

    def _resolved_allow_timeout(self, request: ApiRequest) -> bool:
        if request.allow_retry_on_timeout is not None:
            return request.allow_retry_on_timeout
        return self._allow_retry_on_timeout

    def _can_retry(
        self,
        error_category: ErrorCategory,
        attempt_no: int,
        max_attempts: int,
        *,
        allow_retry_timeout: bool,
    ) -> bool:
        if attempt_no >= max_attempts:
            return False
        if error_category in RETRYABLE_CATEGORIES:
            return True
        return error_category == ErrorCategory.TIMEOUT_UNKNOWN and allow_retry_timeout

    def _compute_delay(
        self,
        error_category: ErrorCategory,
        attempt_no: int,
        retry_after: float | None,
    ) -> float:
        if error_category == ErrorCategory.RATE_LIMITED and retry_after is not None:
            base = min(retry_after, self._backoff_max)
        else:
            base = min(self._backoff_base * (2 ** (attempt_no - 1)), self._backoff_max)
        if self._retry_jitter <= 0:
            return base
        return base + random.uniform(0.0, self._retry_jitter * base)

    def _retry_after_from_response(
        self, resp: httpx.Response, error_category: ErrorCategory
    ) -> float | None:
        if error_category not in (ErrorCategory.RATE_LIMITED, ErrorCategory.PROVIDER_OVERLOADED):
            return None
        return self._adapter.extract_retry_after(dict(resp.headers))

    def _classify_response_error(self, status_code: int, body: str | None) -> ErrorCategory:
        return self._adapter.classify_error(
            status_code=status_code, response_body=body, exc=None
        )

    @staticmethod
    def _safe_message(
        status_code: int | None, body: str | None, exc: BaseException | None
    ) -> str:
        """提取脱敏的错误信息：优先 JSON ``error.message``，截断后脱敏。"""
        msg: str | None = None
        if body:
            parsed = ApiClient._parse_json(body)
            if isinstance(parsed, Mapping):
                err = parsed.get("error")
                if isinstance(err, Mapping):
                    m = err.get("message")
                    if isinstance(m, str):
                        msg = m
            if msg is None:
                msg = body
        if msg is None and exc is not None:
            msg = str(exc)
        if msg is None:
            msg = f"HTTP {status_code}" if status_code is not None else "unknown error"
        # 不包含 headers/keys；截断后经 redact_text 脱敏。
        return redact_text(msg[:_MAX_SAFE_MESSAGE_CHARS])

    @staticmethod
    def _raw_reference(body: str | None) -> str | None:
        """对原始错误体计算受控引用（``sha256:<first16hex>``），绝不存原始体。"""
        if not body:
            return None
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        return f"sha256:{digest[:16]}"

    @staticmethod
    def _parse_json(body: str | None) -> object | None:
        if not body:
            return None
        try:
            return json.loads(body)
        except (json.JSONDecodeError, TypeError):
            return None

    @staticmethod
    def _read_body(resp: httpx.Response) -> str | None:
        try:
            return resp.text
        except Exception:  # noqa: BLE001 - 读取响应体失败不影响主流程
            return None

    @staticmethod
    def _extract_delta(payload: Mapping[str, object]) -> str:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        first = choices[0]
        if not isinstance(first, Mapping):
            return ""
        delta = first.get("delta")
        if not isinstance(delta, Mapping):
            return ""
        content = delta.get("content")
        return content if isinstance(content, str) else ""

    @staticmethod
    def _new_request_id() -> str:
        return uuid.uuid4().hex

    def _log(self, record: RequestRecord) -> None:
        """请求记录（恰好一次）；logger 为 None 时跳过。"""
        if self._logger is not None:
            self._logger.log_request(record)
