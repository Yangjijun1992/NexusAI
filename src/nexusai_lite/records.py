"""统一 API Client 与 Provider Adapter 的共享类型（S1-03）。

本模块定义后续模块（Scheduler、执行器）都会导入的冻结接口：
错误分类、用量、成本估算、请求、尝试与请求记录。字段名与类型为冻结
接口，后续 Wave 依赖它们，修改须谨慎并保持向后兼容。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Protocol


class ErrorCategory(str, enum.Enum):  # noqa: UP042 - 冻结接口要求 str+Enum 基类
    """统一的供应商错误分类（reference §0.3 原则 8 / §4.1）。

    适配器把供应商特有的状态码 / 异常映射到这些稳定分类，业务模块据此
    决定是否重试、是否告警，避免每个业务模块理解各家错误语义。
    """

    AUTHENTICATION_ERROR = "authentication_error"
    PERMISSION_ERROR = "permission_error"
    RATE_LIMITED = "rate_limited"
    PROVIDER_OVERLOADED = "provider_overloaded"
    PROVIDER_SERVER_ERROR = "provider_server_error"
    NETWORK_ERROR = "network_error"
    TIMEOUT_UNKNOWN = "timeout_unknown"
    INVALID_REQUEST = "invalid_request"
    BUDGET_EXCEEDED = "budget_exceeded"
    CANCELLED = "cancelled"
    UNKNOWN_ERROR = "unknown_error"


#: 可安全重试的错误分类（有限重试受次数/时间/成本上限约束，reference §5）。
RETRYABLE_CATEGORIES: frozenset[ErrorCategory] = frozenset(
    {
        ErrorCategory.RATE_LIMITED,
        ErrorCategory.PROVIDER_OVERLOADED,
        ErrorCategory.PROVIDER_SERVER_ERROR,
        ErrorCategory.NETWORK_ERROR,
    }
)


@dataclass(frozen=True)
class Usage:
    """一次响应的 Token 用量（供应商可能不全提供）。"""

    input_tokens: int
    output_tokens: int
    total_tokens: int | None = None


@dataclass(frozen=True)
class CostEstimate:
    """一次响应的成本估算结果。"""

    estimated_cost: float | None
    cost_status: str
    pricing_version: str | None = None


@dataclass(frozen=True)
class ApiRequest:
    """统一 API 请求参数（业务模块不得各自直接调用供应商 SDK）。"""

    model: str
    messages: list[dict[str, Any]]
    stream: bool = False
    max_tokens: int | None = None
    temperature: float | None = None
    timeout_seconds: float | None = None
    max_retries: int | None = None
    task_id: str | None = None
    idempotency_key: str | None = None
    cost_limit: float | None = None
    allow_retry_on_timeout: bool | None = None
    extra_body: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AttemptRecord:
    """单次执行尝试的记录（供审计与去重）。"""

    attempt_no: int
    started_at: str
    ended_at: str
    status_code: int | None
    error_category: ErrorCategory | None
    retry_reason: str | None
    may_be_billed: bool
    latency_ms: int


@dataclass(frozen=True)
class RequestRecord:
    """一次 ``chat()``/``chat_stream()`` 调用的完整请求记录。

    ``safe_error_message`` 与 ``raw_error_reference`` 遵循 §4.3：业务数据库
    只保存脱敏的错误信息与受控的原始错误引用，绝不保存原始错误体或密钥。
    """

    request_id: str
    provider: str
    model: str
    started_at: str
    ended_at: str
    latency_ms: int
    status_code: int | None
    retry_count: int
    input_tokens: int | None
    output_tokens: int | None
    estimated_cost: float | None
    cost_status: str
    error_category: ErrorCategory | None
    safe_error_message: str | None
    raw_error_reference: str | None
    streaming: bool
    task_id: str | None = None
    attempts: tuple[AttemptRecord, ...] = ()


class RequestLogger(Protocol):
    """请求记录器的协议：业务模块通过它持久化请求记录。"""

    def log_request(self, record: RequestRecord) -> None:
        """持久化一条请求记录；实现方负责写入存储。"""
