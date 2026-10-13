"""429/503 退避与有限重试策略（S2-04）。

实现 reference.md F5 与 §12 规则 4：每次重试都有明确原因、次数上限、时间上限与
成本上限；不确定是否已产生外部副作用时（``TIMEOUT_UNKNOWN``）默认不重试，仅当
显式 ``allow_retry_on_timeout`` 时才重试。429 优先遵守官方 ``Retry-After``；
否则用有限指数退避加随机抖动。

本模块为纯函数 / 确定性策略，不含任何调度器或任务状态机概念（Wave C 会将其
集成进 off-peak 调度器），保持公共 API 稳定。
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from nexusai_lite.records import (
    RETRYABLE_CATEGORIES,
    ErrorCategory,
)

#: 明显不可重试的错误分类：重试不能改变其结果的 4xx / 预算 / 取消 / 未知错误。
_NON_RETRYABLE_CATEGORIES: frozenset[ErrorCategory] = frozenset(
    {
        ErrorCategory.AUTHENTICATION_ERROR,
        ErrorCategory.PERMISSION_ERROR,
        ErrorCategory.INVALID_REQUEST,
        ErrorCategory.BUDGET_EXCEEDED,
        ErrorCategory.CANCELLED,
        ErrorCategory.UNKNOWN_ERROR,
    }
)

#: 关键词料里用重试头（``Retry-After``）的错误分类。
_RETRY_AFTER_CATEGORIES: frozenset[ErrorCategory] = frozenset(
    {ErrorCategory.RATE_LIMITED, ErrorCategory.PROVIDER_OVERLOADED}
)


def is_retryable(error_category: ErrorCategory, *, allow_timeout: bool) -> bool:
    """判断该错误分类是否可安全重试。

    Args:
        error_category: 供应商错误分类。
        allow_timeout: 是否允许重试 ``TIMEOUT_UNKNOWN``。

    Returns:
        可重试返回 ``True``；否则返回 ``False``。
    """
    if error_category in RETRYABLE_CATEGORIES:
        return True
    return error_category == ErrorCategory.TIMEOUT_UNKNOWN and allow_timeout


@dataclass(frozen=True)
class RetryDecision:
    """一次重试判定结果。

    Attributes:
        should_retry: 是否应重试。
        delay_seconds: 建议的退避等待秒数；``should_retry=False`` 时为 ``None``。
        reason: 判定原因（可追踪，直接写入审计/任务尝试记录）。
        stop_and_alert: 是否应立即停止并告警（如认证错误），此时 ``should_retry=False``。
    """

    should_retry: bool
    delay_seconds: float | None
    reason: str
    stop_and_alert: bool = False


@dataclass(frozen=True)
class RetryPolicy:
    """有限重试策略参数（reference §0.3 原则 5、F5、§12 规则 4）。

    Attributes:
        max_attempts: 最大尝试次数（含首次）。
        max_total_seconds: 累计重试等待时间上限（秒）。
        max_total_cost: 累计重试成本上限；``None`` 表示未设置。
        backoff_base_seconds: 指数退避基数。
        backoff_max_seconds: 指数退避上限。
        jitter_ratio: 抖动比例（0..1）；0 表示无抖动（测试用确定性）。
        allow_retry_on_timeout: 是否允许重试 ``TIMEOUT_UNKNOWN``。
    """

    max_attempts: int = 2
    max_total_seconds: float = 60.0
    max_total_cost: float | None = None
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 30.0
    jitter_ratio: float = 0.25
    allow_retry_on_timeout: bool = False

    def decide(  # noqa: PLR0911 - 每种上限/不可重试分类各一个返回点，属规定判定逻辑
        self,
        *,
        error_category: ErrorCategory,
        attempt: int,
        elapsed_seconds: float,
        cost_so_far: float = 0.0,
        retry_after: float | None = None,
    ) -> RetryDecision:
        """对一次失败尝试做出重试判定（确定性成分；抖动依赖 :mod:`random`）。

        Args:
            error_category: 本尝试的错误分类。
            attempt: 尝试序号（从 1 开始的第几次尝试）。
            elapsed_seconds: 本任务已累计等待/花费的秒数。
            cost_so_far: 已累计成本（用于成本上限，默认 0.0）。
            retry_after: 服务端 ``Retry-After`` 秒数；仅 ``RATE_LIMITED`` 与
                ``PROVIDER_OVERLOADED`` 有意义。

        Returns:
            :class:`RetryDecision`。
        """
        # 1) 不可重试分类。
        if error_category in _NON_RETRYABLE_CATEGORIES:
            stop_and_alert = error_category == ErrorCategory.AUTHENTICATION_ERROR
            # F5: 认证错误优先停止并告警，不得反复尝试同一失效密钥。
            reason = "not_retryable_non_retryable_category"
            return RetryDecision(
                should_retry=False,
                delay_seconds=None,
                reason=reason,
                stop_and_alert=stop_and_alert,
            )

        # 2) 超时仅在显式允许时重试（FILTER：不确定是否产生副作用，保守处理）。
        if error_category == ErrorCategory.TIMEOUT_UNKNOWN and not self.allow_retry_on_timeout:
            return RetryDecision(
                should_retry=False, delay_seconds=None, reason="timeout_unknown_not_allowed"
            )

        # 3) RETRYABLE_CATEGORIES 之外的任何分类（防御）一律不重试。
        if not is_retryable(error_category, allow_timeout=self.allow_retry_on_timeout):
            return RetryDecision(
                should_retry=False, delay_seconds=None, reason="not_retryable_category"
            )

        # 4) 次数上限。
        if attempt >= self.max_attempts:
            return RetryDecision(
                should_retry=False, delay_seconds=None, reason="max_attempts_reached"
            )

        # 5) 时间上限。
        if elapsed_seconds >= self.max_total_seconds:
            return RetryDecision(
                should_retry=False, delay_seconds=None, reason="max_total_seconds_reached"
            )

        # 6) 成本上限。
        if self.max_total_cost is not None and cost_so_far >= self.max_total_cost:
            return RetryDecision(
                should_retry=False, delay_seconds=None, reason="max_total_cost_reached"
            )

        # 7) 计算退避延迟。
        delay = self._compute_delay(error_category, attempt, retry_after)
        return RetryDecision(
                should_retry=True, delay_seconds=delay, reason="retry_allowed"
            )

    def _compute_delay(
        self,
        error_category: ErrorCategory,
        attempt: int,
        retry_after: float | None,
    ) -> float:
        """计算建议的退避等待秒数（有限指数退避 + 抖动，尊重 ``Retry-After``）。"""
        if error_category in _RETRY_AFTER_CATEGORIES and retry_after is not None:
            base = min(max(retry_after, 0.0), self.backoff_max_seconds)
        else:
            base = min(self.backoff_base_seconds * (2 ** (attempt - 1)), self.backoff_max_seconds)
        if self.jitter_ratio <= 0 or base <= 0:
            return base
        return base + random.uniform(0.0, self.jitter_ratio * base)  # noqa: S311 - 调度退避抖动，非安全用途
