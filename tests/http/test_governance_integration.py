"""Integration tests for HTTP client governance pipeline.

Verifies the complete governance flow: capability gating by HTTP method,
audit record accumulation with redacted URLs, auth header resolution via
SecretProvider, rate limiting enforcement, and bootstrap wiring.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
import pytest

from zeroth.core.http.client import ResilientHttpClient
from zeroth.core.http.errors import HttpClientError, HttpRateLimitError
from zeroth.core.http.models import AuthType, EndpointConfig, HttpClientSettings
from zeroth.core.policy.models import Capability


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class MockSecretProvider:
    """Minimal SecretProvider stand-in that resolves from a dict."""

    secrets: dict[str, str] = field(default_factory=dict)

    def resolve(self, secret_ref: str) -> str | None:
        return self.secrets.get(secret_ref)

    def resolve_many(self, refs: list[str]) -> dict[str, str]:
        return {ref: val for ref in refs if (val := self.secrets.get(ref)) is not None}


def _mock_transport(
    *,
    status: int = 200,
    body: bytes = b'{"ok": true}',
) -> httpx.MockTransport:
    """Return a simple mock transport with canned responses."""
    return httpx.MockTransport(
        lambda request: httpx.Response(status, content=body),
    )


def _header_capture_transport(
    captured: dict[str, str],
    *,
    status: int = 200,
) -> httpx.MockTransport:
    """Return a mock transport that captures request headers into *captured*."""

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.headers))
        return httpx.Response(status)

    return httpx.MockTransport(handler)


def _make_client(
    settings: HttpClientSettings | None = None,
    transport: httpx.MockTransport | None = None,
    secret_provider: MockSecretProvider | None = None,
) -> ResilientHttpClient:
    """Build a ResilientHttpClient wired to a mock transport."""
    s = settings or HttpClientSettings()
    client = ResilientHttpClient(s, secret_provider=secret_provider)
    client._client = httpx.AsyncClient(transport=transport or _mock_transport())
    return client


# ---------------------------------------------------------------------------
# Capability gating tests
# ---------------------------------------------------------------------------


class TestCapabilityGating:
    """Capability gating blocks requests missing required capabilities."""

    @pytest.mark.asyncio
    async def test_get_succeeds_with_network_read_capability(self) -> None:
        client = _make_client()
        resp = await client.get(
            "https://api.example.com/v1/data",
            effective_capabilities={Capability.NETWORK_READ, Capability.EXTERNAL_API_CALL},
        )
        assert resp.status_code == 200
        await client.aclose()

    @pytest.mark.asyncio
    async def test_get_fails_without_network_read_capability(self) -> None:
        client = _make_client()
        with pytest.raises(HttpClientError, match="capability"):
            await client.get(
                "https://api.example.com/v1/data",
                effective_capabilities={Capability.NETWORK_WRITE},
            )
        await client.aclose()

    @pytest.mark.asyncio
    async def test_post_succeeds_with_network_write_capability(self) -> None:
        client = _make_client()
        resp = await client.post(
            "https://api.example.com/v1/data",
            effective_capabilities={Capability.NETWORK_WRITE, Capability.EXTERNAL_API_CALL},
        )
        assert resp.status_code == 200
        await client.aclose()

    @pytest.mark.asyncio
    async def test_request_skips_capability_check_when_none(self) -> None:
        """Backward compat: effective_capabilities=None skips the check entirely."""
        client = _make_client()
        resp = await client.get("https://api.example.com/v1/data")
        assert resp.status_code == 200
        await client.aclose()


# ---------------------------------------------------------------------------
# Audit record accumulation tests
# ---------------------------------------------------------------------------


class TestAuditRecordAccumulation:
    """HttpCallRecord entries accumulate with correct redaction and metadata."""

    @pytest.mark.asyncio
    async def test_call_record_has_redacted_url(self) -> None:
        client = _make_client()
        await client.get("https://api.example.com/v1/data?key=secret&token=abc")
        records = client.drain_call_records()
        assert len(records) == 1
        assert records[0].url == "https://api.example.com/v1/data"
        assert "secret" not in records[0].url
        assert "token" not in records[0].url
        await client.aclose()

    @pytest.mark.asyncio
    async def test_call_record_captures_method_status_latency(self) -> None:
        client = _make_client()
        await client.get("https://api.example.com/v1/data")
        records = client.drain_call_records()
        assert len(records) == 1
        assert records[0].method == "GET"
        assert records[0].status_code == 200
        assert records[0].latency_ms > 0
        assert records[0].response_size_bytes is not None
        await client.aclose()

    @pytest.mark.asyncio
    async def test_drain_clears_records(self) -> None:
        client = _make_client()
        await client.get("https://api.example.com/a")
        await client.get("https://api.example.com/b")
        records = client.drain_call_records()
        assert len(records) == 2
        # Subsequent drain returns empty
        assert client.drain_call_records() == []
        await client.aclose()


# ---------------------------------------------------------------------------
# Auth resolution tests
# ---------------------------------------------------------------------------


class TestAuthResolution:
    """Auth headers resolved from SecretProvider for bearer/API-key/custom."""

    @pytest.mark.asyncio
    async def test_bearer_auth_injected(self) -> None:
        captured: dict[str, str] = {}
        provider = MockSecretProvider(secrets={"MY_TOKEN": "sk-123"})
        client = _make_client(
            transport=_header_capture_transport(captured),
            secret_provider=provider,
        )
        endpoint_cfg = EndpointConfig(
            secret_key="MY_TOKEN",
            auth_type=AuthType.BEARER,
        )
        await client.get(
            "https://api.example.com/v1/data",
            endpoint_config=endpoint_cfg,
        )
        assert captured.get("authorization") == "Bearer sk-123"
        await client.aclose()

    @pytest.mark.asyncio
    async def test_api_key_auth_injected(self) -> None:
        captured: dict[str, str] = {}
        provider = MockSecretProvider(secrets={"MY_KEY": "api-key-value"})
        client = _make_client(
            transport=_header_capture_transport(captured),
            secret_provider=provider,
        )
        endpoint_cfg = EndpointConfig(
            secret_key="MY_KEY",
            auth_type=AuthType.API_KEY,
            auth_header_name="X-API-Key",
        )
        await client.get(
            "https://api.example.com/v1/data",
            endpoint_config=endpoint_cfg,
        )
        assert captured.get("x-api-key") == "api-key-value"
        await client.aclose()

    @pytest.mark.asyncio
    async def test_no_auth_when_no_secret_key(self) -> None:
        captured: dict[str, str] = {}
        client = _make_client(transport=_header_capture_transport(captured))
        await client.get(
            "https://api.example.com/v1/data",
            endpoint_config=EndpointConfig(),
        )
        assert "authorization" not in captured
        assert "x-api-key" not in captured
        await client.aclose()


# ---------------------------------------------------------------------------
# Rate limiting test
# ---------------------------------------------------------------------------


class TestRateLimiting:
    """Rate limiter prevents request when tokens exhausted."""

    @pytest.mark.asyncio
    async def test_rate_limit_rejects_when_exhausted(self) -> None:
        settings = HttpClientSettings(
            default_rate_limit_burst=1,
            default_rate_limit_rate=0.001,
        )
        client = _make_client(settings=settings)
        # First request consumes the single burst token
        await client.get("https://api.example.com/v1/data")
        # Second immediate request should be rejected
        with pytest.raises(HttpRateLimitError):
            await client.get("https://api.example.com/v1/data")
        await client.aclose()


# ---------------------------------------------------------------------------
# Multiple records accumulation
# ---------------------------------------------------------------------------


class TestMultipleRecords:
    """Multiple sequential requests accumulate multiple HttpCallRecord entries."""

    @pytest.mark.asyncio
    async def test_multiple_requests_accumulate_records(self) -> None:
        client = _make_client()
        await client.get("https://api.example.com/a")
        await client.post("https://api.example.com/b")
        await client.get("https://api.example.com/c")
        records = client.drain_call_records()
        assert len(records) == 3
        assert records[0].method == "GET"
        assert records[1].method == "POST"
        assert records[2].method == "GET"
        await client.aclose()


# ---------------------------------------------------------------------------
# Full pipeline test
# ---------------------------------------------------------------------------


class TestFullGovernancePipeline:
    """Full pipeline: auth + capability + rate limit + audit all in single request."""

    @pytest.mark.asyncio
    async def test_full_governance_pipeline(self) -> None:
        captured: dict[str, str] = {}
        provider = MockSecretProvider(secrets={"SVC_TOKEN": "bearer-val-123"})
        client = _make_client(
            transport=_header_capture_transport(captured),
            secret_provider=provider,
        )
        endpoint_cfg = EndpointConfig(
            secret_key="SVC_TOKEN",
            auth_type=AuthType.BEARER,
        )
        resp = await client.get(
            "https://api.example.com/v1/resource?secret=hidden",
            endpoint_config=endpoint_cfg,
            effective_capabilities={Capability.NETWORK_READ, Capability.EXTERNAL_API_CALL},
        )
        # Response succeeds
        assert resp.status_code == 200
        # Auth header was injected
        assert captured.get("authorization") == "Bearer bearer-val-123"
        # Audit record captured
        records = client.drain_call_records()
        assert len(records) == 1
        assert records[0].url == "https://api.example.com/v1/resource"
        assert records[0].method == "GET"
        assert records[0].status_code == 200
        assert "secret" not in records[0].url
        await client.aclose()


# ---------------------------------------------------------------------------
# Bootstrap wiring test
# ---------------------------------------------------------------------------


class TestBootstrapWiring:
    """HttpClientSettings is available on ZerothSettings."""

    def test_http_client_settings_in_zeroth_settings(self) -> None:
        from zeroth.core.config.settings import ZerothSettings

        settings = ZerothSettings()
        assert hasattr(settings, "http_client")
        assert isinstance(settings.http_client, HttpClientSettings)
