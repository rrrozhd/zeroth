"""Unit tests for retry error classification and backoff computation."""

from zeroth.core.agent_runtime.retry import compute_backoff_delay, is_retryable_provider_error


class TestIsRetryableProviderError:
    def test_rate_limit_error_is_retryable(self):
        import litellm

        exc = litellm.RateLimitError(
            message="rate limited", llm_provider="openai", model="test"
        )
        assert is_retryable_provider_error(exc) is True

    def test_service_unavailable_is_retryable(self):
        import litellm

        exc = litellm.ServiceUnavailableError(
            message="503", llm_provider="openai", model="test"
        )
        assert is_retryable_provider_error(exc) is True

    def test_timeout_is_retryable(self):
        import litellm

        exc = litellm.Timeout(
            message="timeout", model="test", llm_provider="openai"
        )
        assert is_retryable_provider_error(exc) is True

    def test_internal_server_error_is_retryable(self):
        import litellm

        exc = litellm.InternalServerError(
            message="500", llm_provider="openai", model="test"
        )
        assert is_retryable_provider_error(exc) is True

    def test_authentication_error_is_not_retryable(self):
        import litellm

        exc = litellm.AuthenticationError(
            message="401", llm_provider="openai", model="test"
        )
        assert is_retryable_provider_error(exc) is False

    def test_bad_request_error_is_not_retryable(self):
        import litellm

        exc = litellm.BadRequestError(
            message="400", model="test", llm_provider="openai"
        )
        assert is_retryable_provider_error(exc) is False

    def test_generic_exception_is_not_retryable(self):
        exc = ValueError("something went wrong")
        assert is_retryable_provider_error(exc) is False


class TestComputeBackoffDelay:
    def test_first_retry_uses_base_delay(self):
        delay = compute_backoff_delay(2, base_delay=1.0, max_delay=60.0, jitter=False)
        assert delay == 1.0

    def test_second_retry_doubles(self):
        delay = compute_backoff_delay(3, base_delay=1.0, max_delay=60.0, jitter=False)
        assert delay == 2.0

    def test_third_retry_quadruples(self):
        delay = compute_backoff_delay(4, base_delay=1.0, max_delay=60.0, jitter=False)
        assert delay == 4.0

    def test_delay_capped_at_max(self):
        delay = compute_backoff_delay(20, base_delay=1.0, max_delay=60.0, jitter=False)
        assert delay == 60.0

    def test_jitter_produces_value_in_range(self):
        delays = [
            compute_backoff_delay(3, base_delay=1.0, max_delay=60.0, jitter=True)
            for _ in range(100)
        ]
        assert all(0 <= d <= 2.0 for d in delays)
        # With 100 samples, at least some should differ (not all identical)
        assert len(set(round(d, 6) for d in delays)) > 1

    def test_attempt_1_returns_zero(self):
        delay = compute_backoff_delay(1, base_delay=1.0, max_delay=60.0, jitter=False)
        assert delay == 0.0
