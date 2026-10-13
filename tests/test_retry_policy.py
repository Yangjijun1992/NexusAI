"""有限重试策略测试（S2-04）。

确定性：``jitter_ratio=0`` 时退避无抖动，可精确断言延迟。覆盖 F5 与 §12 规则 4：
``Retry-After`` 尊重、指数退避、不可重试 4xx、认证错误停止并告警、超时仅在允许时
重试、次数/时间/成本上限。
"""

from __future__ import annotations

from nexusai_lite.records import ErrorCategory
from nexusai_lite.retry_policy import RetryDecision, RetryPolicy, is_retryable

#: 不可重试分类（F5：明显不可重试的 4xx 等不自动重试）。
_NON_RETRYABLE = (
    ErrorCategory.AUTHENTICATION_ERROR,
    ErrorCategory.PERMISSION_ERROR,
    ErrorCategory.INVALID_REQUEST,
    ErrorCategory.BUDGET_EXCEEDED,
    ErrorCategory.CANCELLED,
    ErrorCategory.UNKNOWN_ERROR,
)


def test_is_retryable_rate_limited() -> None:
    """RATE_LIMITED 应始终可重试（无论是否允许超时重试）。"""
    assert is_retryable(ErrorCategory.RATE_LIMITED, allow_timeout=False)
    assert is_retryable(ErrorCategory.RATE_LIMITED, allow_timeout=True)


def test_is_retryable_provider_overloaded() -> None:
    """PROVIDER_OVERLOADED 应可重试（503 服务过载信号）。"""
    assert is_retryable(ErrorCategory.PROVIDER_OVERLOADED, allow_timeout=True)


def test_is_retryable_server_network() -> None:
    """PROVIDER_SERVER_ERROR / NETWORK_ERROR 应可重试。"""
    assert is_retryable(ErrorCategory.PROVIDER_SERVER_ERROR, allow_timeout=False)
    assert is_retryable(ErrorCategory.NETWORK_ERROR, allow_timeout=False)


def test_is_retryable_timeout_only_when_allowed() -> None:
    """TIMEOUT_UNKNOWN 仅在显式允许时重试（F5：不确定副作用不得盲目重试）。"""
    assert not is_retryable(ErrorCategory.TIMEOUT_UNKNOWN, allow_timeout=False)
    assert is_retryable(ErrorCategory.TIMEOUT_UNKNOWN, allow_timeout=True)


def test_is_retryable_non_retryable_categories() -> None:
    """不可重试分类（4xx/预算/取消/未知）应始终不可重试。"""
    for category in _NON_RETRYABLE:
        assert not is_retryable(category, allow_timeout=True)


def test_429_retry_after_honored() -> None:
    """429 应尊重 Retry-After（封顶到 backoff_max）。"""
    policy = RetryPolicy(jitter_ratio=0.0)
    decision = policy.decide(
        error_category=ErrorCategory.RATE_LIMITED,
        attempt=1,
        elapsed_seconds=0.0,
        retry_after=5.0,
    )
    assert decision.should_retry
    assert decision.delay_seconds == 5.0
    assert not decision.stop_and_alert


def test_429_retry_after_capped_to_max() -> None:
    """429 的 Retry-After 应被 backoff_max_seconds 封顶。"""
    policy = RetryPolicy(backoff_max_seconds=30.0, jitter_ratio=0.0)
    decision = policy.decide(
        error_category=ErrorCategory.RATE_LIMITED,
        attempt=1,
        elapsed_seconds=0.0,
        retry_after=120.0,
    )
    assert decision.should_retry
    assert decision.delay_seconds == 30.0


def test_503_retry_after_honored() -> None:
    """503（PROVIDER_OVERLOADED）应尊重 Retry-After。"""
    policy = RetryPolicy(max_attempts=5, jitter_ratio=0.0)
    decision = policy.decide(
        error_category=ErrorCategory.PROVIDER_OVERLOADED,
        attempt=2,
        elapsed_seconds=0.0,
        retry_after=8.0,
    )
    assert decision.should_retry
    assert decision.delay_seconds == 8.0


def test_429_exponential_without_retry_after() -> None:
    """无 Retry-After 的 429 应为指数退避：base*2**(attempt-1)。"""
    policy = RetryPolicy(backoff_base_seconds=1.0, max_attempts=5, jitter_ratio=0.0)
    d1 = policy.decide(error_category=ErrorCategory.RATE_LIMITED, attempt=1, elapsed_seconds=0.0)
    d2 = policy.decide(error_category=ErrorCategory.RATE_LIMITED, attempt=2, elapsed_seconds=0.0)
    d3 = policy.decide(error_category=ErrorCategory.RATE_LIMITED, attempt=3, elapsed_seconds=0.0)
    assert d1.delay_seconds == 1.0
    assert d2.delay_seconds == 2.0
    assert d3.delay_seconds == 4.0


def test_5xx_exponential() -> None:
    """5xx（PROVIDER_SERVER_ERROR）应为指数退避。"""
    policy = RetryPolicy(backoff_base_seconds=2.0, max_attempts=5, jitter_ratio=0.0)
    d1 = policy.decide(
        error_category=ErrorCategory.PROVIDER_SERVER_ERROR, attempt=1, elapsed_seconds=0.0
    )
    d2 = policy.decide(
        error_category=ErrorCategory.PROVIDER_SERVER_ERROR, attempt=2, elapsed_seconds=0.0
    )
    assert d1.delay_seconds == 2.0
    assert d2.delay_seconds == 4.0


def test_exponential_capped_to_max() -> None:
    """指数退避应封顶到 backoff_max_seconds。"""
    policy = RetryPolicy(
        backoff_base_seconds=1.0, backoff_max_seconds=5.0, max_attempts=10, jitter_ratio=0.0
    )
    d4 = policy.decide(
        error_category=ErrorCategory.NETWORK_ERROR, attempt=4, elapsed_seconds=0.0
    )
    d5 = policy.decide(
        error_category=ErrorCategory.NETWORK_ERROR, attempt=5, elapsed_seconds=0.0
    )
    assert d4.delay_seconds == 5.0
    assert d5.delay_seconds == 5.0


def test_400_not_retried() -> None:
    """400（INVALID_REQUEST）不得自动重试。"""
    decision = RetryPolicy(jitter_ratio=0.0).decide(
        error_category=ErrorCategory.INVALID_REQUEST, attempt=1, elapsed_seconds=0.0
    )
    assert not decision.should_retry
    assert decision.delay_seconds is None
    assert not decision.stop_and_alert


def test_401_stops_and_alerts() -> None:
    """401（AUTHENTICATION_ERROR）应停止并告警（F5：不得反复尝试失效密钥）。"""
    decision = RetryPolicy(jitter_ratio=0.0).decide(
        error_category=ErrorCategory.AUTHENTICATION_ERROR, attempt=1, elapsed_seconds=0.0
    )
    assert not decision.should_retry
    assert decision.stop_and_alert
    assert decision.delay_seconds is None


def test_permission_not_retried() -> None:
    """403（PERMISSION_ERROR）不得重试。"""
    decision = RetryPolicy(jitter_ratio=0.0).decide(
        error_category=ErrorCategory.PERMISSION_ERROR, attempt=1, elapsed_seconds=0.0
    )
    assert not decision.should_retry


def test_timeout_not_retried_by_default() -> None:
    """TIMEOUT_UNKNOWN 默认不得重试。"""
    decision = RetryPolicy(jitter_ratio=0.0).decide(
        error_category=ErrorCategory.TIMEOUT_UNKNOWN, attempt=1, elapsed_seconds=0.0
    )
    assert not decision.should_retry


def test_timeout_retried_when_allowed() -> None:
    """显式允许超时重试时应重试。"""
    decision = RetryPolicy(allow_retry_on_timeout=True, jitter_ratio=0.0).decide(
        error_category=ErrorCategory.TIMEOUT_UNKNOWN, attempt=1, elapsed_seconds=0.0
    )
    assert decision.should_retry


def test_max_attempts_blocks_retry() -> None:
    """达到次数上限后不再重试。"""
    policy = RetryPolicy(max_attempts=2, jitter_ratio=0.0)
    allowed = policy.decide(
        error_category=ErrorCategory.RATE_LIMITED, attempt=1, elapsed_seconds=0.0
    )
    blocked = policy.decide(
        error_category=ErrorCategory.RATE_LIMITED, attempt=2, elapsed_seconds=0.0
    )
    assert allowed.should_retry
    assert not blocked.should_retry


def test_max_total_seconds_blocks_retry() -> None:
    """达到时间上限后不再重试。"""
    policy = RetryPolicy(max_total_seconds=10.0, jitter_ratio=0.0)
    allowed = policy.decide(
        error_category=ErrorCategory.RATE_LIMITED, attempt=1, elapsed_seconds=5.0
    )
    blocked = policy.decide(
        error_category=ErrorCategory.RATE_LIMITED, attempt=1, elapsed_seconds=10.0
    )
    assert allowed.should_retry
    assert not blocked.should_retry


def test_max_total_cost_blocks_retry() -> None:
    """达到成本上限后不再重试。"""
    policy = RetryPolicy(max_total_cost=3.0, jitter_ratio=0.0)
    allowed = policy.decide(
        error_category=ErrorCategory.RATE_LIMITED, attempt=1, elapsed_seconds=0.0, cost_so_far=2.0
    )
    blocked = policy.decide(
        error_category=ErrorCategory.RATE_LIMITED, attempt=1, elapsed_seconds=0.0, cost_so_far=3.0
    )
    assert allowed.should_retry
    assert not blocked.should_retry


def test_missing_max_total_cost_does_not_block() -> None:
    """未设置成本上限（None）时不应因成本阻断重试。"""
    policy = RetryPolicy(max_total_cost=None, jitter_ratio=0.0)
    decision = policy.decide(
        error_category=ErrorCategory.RATE_LIMITED, attempt=1, elapsed_seconds=0.0, cost_so_far=1e9
    )
    assert decision.should_retry


def test_reason_always_present() -> None:
    """每次判定都应有可追踪的 reason。"""
    blocked = RetryPolicy(jitter_ratio=0.0).decide(
        error_category=ErrorCategory.INVALID_REQUEST, attempt=1, elapsed_seconds=0.0
    )
    allowed = RetryPolicy(jitter_ratio=0.0).decide(
        error_category=ErrorCategory.RATE_LIMITED, attempt=1, elapsed_seconds=0.0
    )
    assert isinstance(blocked.reason, str)
    assert blocked.reason
    assert isinstance(allowed.reason, str)
    assert allowed.reason == "retry_allowed"


def test_decision_fields() -> None:
    """RetryDecision 应持有约定的字段。"""
    decision = RetryDecision(should_retry=True, delay_seconds=1.0, reason="retry_allowed")
    assert decision.should_retry
    assert decision.delay_seconds == 1.0
    assert decision.reason == "retry_allowed"
    assert decision.stop_and_alert is False
