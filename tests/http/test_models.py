"""Tests for zeroth.core.http.models — settings, config, call records, URL redaction."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from zeroth.core.http.models import (
    AuthType,
    EndpointConfig,
    HttpCallRecord,
    HttpClientSettings,
    redact_url,
)


class TestHttpClientSettings:
    """HttpClientSettings defaults and validation."""

    def test_defaults(self) -> None:
        s = HttpClientSettings()
        assert s.max_retries == 3
        assert s.retry_backoff_base == 0.5
        assert s.retry_max_delay == 60.0
        assert s.retryable_status_codes == {408, 429, 500, 502, 503, 504}
        assert s.circuit_breaker_threshold == 5
        assert s.circuit_breaker_reset_timeout == 30.0
        assert s.pool_max_connections == 100
        assert s.pool_max_keepalive == 20
        assert s.pool_keepalive_expiry == 5.0
        assert s.default_timeout == 30.0
        assert s.default_rate_limit_rate == 10.0
        assert s.default_rate_limit_burst == 20

    def test_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            HttpClientSettings(unknown_field="boom")  # type: ignore[call-arg]


class TestEndpointConfig:
    """EndpointConfig optional overrides."""

    def test_all_none_defaults(self) -> None:
        cfg = EndpointConfig()
        assert cfg.max_retries is None
        assert cfg.retryable_status_codes is None
        assert cfg.timeout is None
        assert cfg.secret_key is None
        assert cfg.auth_type is None
        assert cfg.auth_header_name == "Authorization"
        assert cfg.rate_limit_rate is None
        assert cfg.rate_limit_burst is None

    def test_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            EndpointConfig(bogus=True)  # type: ignore[call-arg]

    def test_override_values(self) -> None:
        cfg = EndpointConfig(
            max_retries=5,
            timeout=10.0,
            secret_key="MY_KEY",
            auth_type=AuthType.BEARER,
            rate_limit_rate=5.0,
            rate_limit_burst=10,
        )
        assert cfg.max_retries == 5
        assert cfg.auth_type == AuthType.BEARER


class TestAuthType:
    """AuthType enum members."""

    def test_values(self) -> None:
        assert AuthType.BEARER == "bearer"
        assert AuthType.API_KEY == "api_key"
        assert AuthType.CUSTOM_HEADER == "custom_header"


class TestHttpCallRecord:
    """HttpCallRecord creation and defaults."""

    def test_creation(self) -> None:
        rec = HttpCallRecord(url="https://example.com/api", method="GET", latency_ms=42.5)
        assert rec.url == "https://example.com/api"
        assert rec.method == "GET"
        assert rec.status_code is None
        assert rec.latency_ms == 42.5
        assert rec.response_size_bytes is None
        assert rec.retry_count == 0
        assert rec.circuit_breaker_state is None
        assert rec.error is None

    def test_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            HttpCallRecord(url="u", method="GET", latency_ms=1.0, junk="x")  # type: ignore[call-arg]


class TestRedactUrl:
    """redact_url strips query params and fragments."""

    def test_strips_query(self) -> None:
        assert redact_url("https://api.example.com/v1/data?key=secret&token=abc") == (
            "https://api.example.com/v1/data"
        )

    def test_strips_fragment(self) -> None:
        assert redact_url("https://example.com/page#section") == "https://example.com/page"

    def test_strips_both(self) -> None:
        assert redact_url("https://example.com/p?q=1#f") == "https://example.com/p"

    def test_no_query_no_fragment(self) -> None:
        assert redact_url("https://example.com/clean") == "https://example.com/clean"

    def test_preserves_path(self) -> None:
        assert redact_url("https://host:8080/a/b/c?x=1") == "https://host:8080/a/b/c"
