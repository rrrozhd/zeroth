"""Tests for zeroth.core.http.client — ResilientHttpClient and settings wiring."""

from __future__ import annotations

import asyncio
import random
from unittest.mock import MagicMock

import httpx
import pytest

from zeroth.core.http.client import ResilientHttpClient
from zeroth.core.http.errors import (
    CircuitOpenError,
    HttpClientError,
    HttpRateLimitError,
    HttpRetryExhaustedError,
)
from zeroth.core.http.models import AuthType, EndpointConfig, HttpClientSettings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_transport(
    responses: list[httpx.Response] | None = None,
    *,
    status: int = 200,
    body: bytes = b'{"ok": true}',
) -> httpx.MockTransport:
    """Return a mock transport that serves canned responses."""
    if responses is not None:
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            idx = min(call_count, len(responses) - 1)
            call_count += 1
            return responses[idx]

        return httpx.MockTransport(handler)

    return httpx.MockTransport(
        lambda request: httpx.Response(status, content=body),
    )


class _FakeSecretProvider:
    """Minimal SecretProvider stand-in that resolves from a dict."""

    def __init__(self, secrets: dict[str, str]) -> None:
        self._secrets = secrets

    def resolve(self, secret_ref: str) -> str | None:
        return self._secrets.get(secret_ref)

    def resolve_many(self, refs: list[str]) -> dict[str, str]:
        return {r: v for r in refs if (v := self._secrets.get(r)) is not None}


# ---------------------------------------------------------------------------
# Basic request flow
# ---------------------------------------------------------------------------


class TestBasicRequests:
    """Successful requests through the client."""

    @pytest.mark.asyncio
    async def test_get_returns_response(self) -> None:
        settings = HttpClientSettings()
        client = ResilientHttpClient(settings)
        client._client = httpx.AsyncClient(transport=_mock_transport())
        resp = await client.get("https://api.example.com/data")
        assert resp.status_code == 200
        await client.aclose()

    @pytest.mark.asyncio
    async def test_post_returns_response(self) -> None:
        settings = HttpClientSettings()
        client = ResilientHttpClient(settings)
        client._client = httpx.AsyncClient(transport=_mock_transport(status=201))
        resp = await client.post("https://api.example.com/data", json={"key": "val"})
        assert resp.status_code == 201
        await client.aclose()

    @pytest.mark.asyncio
    async def test_convenience_methods_delegate(self) -> None:
        """put, patch, delete all delegate to request()."""
        settings = HttpClientSettings()
        client = ResilientHttpClient(settings)
        client._client = httpx.AsyncClient(transport=_mock_transport())
        for method in ("put", "patch", "delete"):
            resp = await getattr(client, method)("https://api.example.com/resource")
            assert resp.status_code == 200
        await client.aclose()


# ---------------------------------------------------------------------------
# Retry behaviour
# ---------------------------------------------------------------------------


class TestRetry:
    """Retry on transient errors and retryable status codes."""

    @pytest.mark.asyncio
    async def test_retries_on_503(self) -> None:
        responses = [
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(200, content=b"ok"),
        ]
        settings = HttpClientSettings(max_retries=3, retry_backoff_base=0.0)
        client = ResilientHttpClient(settings)
        client._client = httpx.AsyncClient(transport=_mock_transport(responses))
        resp = await client.get("https://api.example.com/data")
        assert resp.status_code == 200
        await client.aclose()

    @pytest.mark.asyncio
    async def test_retry_exhaustion_raises(self) -> None:
        responses = [httpx.Response(503)] * 4
        settings = HttpClientSettings(max_retries=3, retry_backoff_base=0.0)
        client = ResilientHttpClient(settings)
        client._client = httpx.AsyncClient(transport=_mock_transport(responses))
        with pytest.raises(HttpRetryExhaustedError):
            await client.get("https://api.example.com/data")
        await client.aclose()

    @pytest.mark.asyncio
    async def test_post_does_not_retry_on_500_by_default(self) -> None:
        """Non-idempotent methods skip status-code retry unless explicit config."""
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(500)

        settings = HttpClientSettings(max_retries=3, retry_backoff_base=0.0)
        client = ResilientHttpClient(settings)
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        resp = await client.post("https://api.example.com/data")
        # Should NOT have retried
        assert call_count == 1
        assert resp.status_code == 500
        await client.aclose()

    @pytest.mark.asyncio
    async def test_post_retries_when_endpoint_config_sets_retryable(self) -> None:
        """POST retries when EndpointConfig explicitly sets retryable_status_codes."""
        responses = [
            httpx.Response(500),
            httpx.Response(200, content=b"ok"),
        ]
        settings = HttpClientSettings(max_retries=3, retry_backoff_base=0.0)
        endpoint_cfg = EndpointConfig(retryable_status_codes={500})
        client = ResilientHttpClient(settings)
        client._client = httpx.AsyncClient(transport=_mock_transport(responses))
        resp = await client.post(
            "https://api.example.com/data",
            endpoint_config=endpoint_cfg,
        )
        assert resp.status_code == 200
        await client.aclose()


# ---------------------------------------------------------------------------
# Backoff
# ---------------------------------------------------------------------------


class TestBackoff:
    """Backoff delay follows the D-05 formula."""

    def test_backoff_formula(self) -> None:
        settings = HttpClientSettings(retry_backoff_base=0.5, retry_max_delay=60.0)
        client = ResilientHttpClient(settings)
        # Seed random for deterministic jitter check
        random.seed(42)
        delay = client._backoff_delay(attempt=2, config=EndpointConfig())
        # Formula: 0.5 * (2 ** 2) + random.uniform(0, 0.1) = 2.0 + jitter
        assert 2.0 <= delay <= 2.1

    def test_backoff_capped_at_max(self) -> None:
        settings = HttpClientSettings(retry_backoff_base=0.5, retry_max_delay=5.0)
        client = ResilientHttpClient(settings)
        delay = client._backoff_delay(attempt=20, config=EndpointConfig())
        assert delay <= 5.1  # 5.0 + max jitter 0.1


# ---------------------------------------------------------------------------
# Circuit breaker integration
# ---------------------------------------------------------------------------


class TestCircuitBreakerIntegration:
    """Circuit breaker opens after threshold failures."""

    @pytest.mark.asyncio
    async def test_opens_after_threshold(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        settings = HttpClientSettings(
            max_retries=0,
            circuit_breaker_threshold=2,
            circuit_breaker_reset_timeout=60.0,
        )
        client = ResilientHttpClient(settings)
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        # First two requests fail and record failures against the circuit breaker
        for _ in range(2):
            with pytest.raises(HttpRetryExhaustedError):
                await client.get("https://api.example.com/data")

        # Third request should immediately get CircuitOpenError
        with pytest.raises(CircuitOpenError):
            await client.get("https://api.example.com/data")
        await client.aclose()


# ---------------------------------------------------------------------------
# Rate limiter integration
# ---------------------------------------------------------------------------


class TestRateLimiterIntegration:
    """Rate limiter rejects when tokens exhausted."""

    @pytest.mark.asyncio
    async def test_rate_limit_exceeded(self) -> None:
        settings = HttpClientSettings(default_rate_limit_rate=0.01, default_rate_limit_burst=1)
        client = ResilientHttpClient(settings)
        client._client = httpx.AsyncClient(transport=_mock_transport())

        # First request OK
        await client.get("https://api.example.com/data")
        # Second should exceed rate limit
        with pytest.raises(HttpRateLimitError):
            await client.get("https://api.example.com/data")
        await client.aclose()


# ---------------------------------------------------------------------------
# Capability gating
# ---------------------------------------------------------------------------


class TestCapabilityGating:
    """Capability check raises when required capabilities missing."""

    @pytest.mark.asyncio
    async def test_missing_capability_raises(self) -> None:
        from zeroth.core.policy.models import Capability

        settings = HttpClientSettings()
        client = ResilientHttpClient(settings)
        client._client = httpx.AsyncClient(transport=_mock_transport())

        # Provide empty capability set
        with pytest.raises(HttpClientError, match="capability"):
            await client.get(
                "https://api.example.com/data",
                effective_capabilities=set(),
            )
        await client.aclose()

    @pytest.mark.asyncio
    async def test_sufficient_capabilities_pass(self) -> None:
        from zeroth.core.policy.models import Capability

        settings = HttpClientSettings()
        client = ResilientHttpClient(settings)
        client._client = httpx.AsyncClient(transport=_mock_transport())

        resp = await client.get(
            "https://api.example.com/data",
            effective_capabilities={Capability.NETWORK_READ, Capability.EXTERNAL_API_CALL},
        )
        assert resp.status_code == 200
        await client.aclose()

    @pytest.mark.asyncio
    async def test_no_capabilities_skips_check(self) -> None:
        """When effective_capabilities is None, capability check is skipped."""
        settings = HttpClientSettings()
        client = ResilientHttpClient(settings)
        client._client = httpx.AsyncClient(transport=_mock_transport())

        resp = await client.get("https://api.example.com/data")
        assert resp.status_code == 200
        await client.aclose()


# ---------------------------------------------------------------------------
# Auth header injection
# ---------------------------------------------------------------------------


class TestAuthInjection:
    """Auth headers resolved from SecretProvider."""

    @pytest.mark.asyncio
    async def test_bearer_token(self) -> None:
        provider = _FakeSecretProvider({"MY_TOKEN": "tok_123"})
        settings = HttpClientSettings()
        endpoint_cfg = EndpointConfig(
            secret_key="MY_TOKEN",
            auth_type=AuthType.BEARER,
        )

        received_headers: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            received_headers.update(dict(request.headers))
            return httpx.Response(200)

        client = ResilientHttpClient(settings, secret_provider=provider)
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await client.get("https://api.example.com/data", endpoint_config=endpoint_cfg)
        assert received_headers.get("authorization") == "Bearer tok_123"
        await client.aclose()

    @pytest.mark.asyncio
    async def test_api_key(self) -> None:
        provider = _FakeSecretProvider({"KEY": "key_abc"})
        settings = HttpClientSettings()
        endpoint_cfg = EndpointConfig(
            secret_key="KEY",
            auth_type=AuthType.API_KEY,
            auth_header_name="X-API-Key",
        )

        received_headers: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            received_headers.update(dict(request.headers))
            return httpx.Response(200)

        client = ResilientHttpClient(settings, secret_provider=provider)
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await client.get("https://api.example.com/data", endpoint_config=endpoint_cfg)
        assert received_headers.get("x-api-key") == "key_abc"
        await client.aclose()

    @pytest.mark.asyncio
    async def test_custom_header(self) -> None:
        provider = _FakeSecretProvider({"TOK": "custom_val"})
        settings = HttpClientSettings()
        endpoint_cfg = EndpointConfig(
            secret_key="TOK",
            auth_type=AuthType.CUSTOM_HEADER,
            auth_header_name="X-Custom-Auth",
        )

        received_headers: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            received_headers.update(dict(request.headers))
            return httpx.Response(200)

        client = ResilientHttpClient(settings, secret_provider=provider)
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await client.get("https://api.example.com/data", endpoint_config=endpoint_cfg)
        assert received_headers.get("x-custom-auth") == "custom_val"
        await client.aclose()


# ---------------------------------------------------------------------------
# Call records
# ---------------------------------------------------------------------------


class TestCallRecords:
    """drain_call_records returns and clears audit records."""

    @pytest.mark.asyncio
    async def test_drain_returns_records_and_clears(self) -> None:
        settings = HttpClientSettings()
        client = ResilientHttpClient(settings)
        client._client = httpx.AsyncClient(transport=_mock_transport())

        await client.get("https://api.example.com/a?secret=1")
        await client.get("https://api.example.com/b#frag")

        records = client.drain_call_records()
        assert len(records) == 2
        # URLs must be redacted
        assert "secret" not in records[0].url
        assert "#frag" not in records[1].url
        assert records[0].method == "GET"
        assert records[0].status_code == 200

        # Second drain should be empty
        assert client.drain_call_records() == []
        await client.aclose()

    @pytest.mark.asyncio
    async def test_records_capture_retry_count(self) -> None:
        responses = [
            httpx.Response(503),
            httpx.Response(200, content=b"ok"),
        ]
        settings = HttpClientSettings(max_retries=3, retry_backoff_base=0.0)
        client = ResilientHttpClient(settings)
        client._client = httpx.AsyncClient(transport=_mock_transport(responses))

        await client.get("https://api.example.com/data")
        records = client.drain_call_records()
        assert len(records) == 1
        assert records[0].retry_count == 1
        await client.aclose()


# ---------------------------------------------------------------------------
# aclose
# ---------------------------------------------------------------------------


class TestAclose:
    """aclose delegates to httpx client."""

    @pytest.mark.asyncio
    async def test_aclose(self) -> None:
        settings = HttpClientSettings()
        client = ResilientHttpClient(settings)
        mock_inner = MagicMock()
        mock_inner.aclose = MagicMock(return_value=asyncio.coroutine(lambda: None)())
        client._client = mock_inner
        await client.aclose()
        mock_inner.aclose.assert_called_once()


# ---------------------------------------------------------------------------
# Settings integration
# ---------------------------------------------------------------------------


class TestSettingsIntegration:
    """HttpClientSettings wired into ZerothSettings."""

    def test_http_client_on_zeroth_settings(self) -> None:
        from zeroth.core.config.settings import ZerothSettings

        s = ZerothSettings()
        assert isinstance(s.http_client, HttpClientSettings)
        assert s.http_client.max_retries == 3
