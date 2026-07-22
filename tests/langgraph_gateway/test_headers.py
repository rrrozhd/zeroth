import httpx
import pytest

from zeroth.core.config.settings import LangGraphGatewaySettings
from zeroth.core.langgraph_gateway.headers import (
    UpstreamCredentialUnavailableError,
    prepare_upstream_request_headers,
    strip_hop_by_hop_headers,
)


class RecordingSecretProvider:
    def __init__(self, value: str | None):
        self.value = value
        self.calls = []

    async def resolve_secret_async(self, logical_name, *, tenant_id=None, deployment_ref=None):
        self.calls.append((logical_name, tenant_id, deployment_ref))
        return self.value


def test_connection_tokens_and_standard_hop_by_hop_headers_are_removed():
    headers = [
        (b"Connection", b"keep-alive, X-Remove"),
        (b"X-Remove", b"secret"),
        (b"Keep-Alive", b"timeout=5"),
        (b"Transfer-Encoding", b"chunked"),
        (b"X-End-To-End", b"first"),
        (b"connection", b"X-Also-Remove"),
        (b"x-also-remove", b"secret-two"),
        (b"X-End-To-End", b"second"),
    ]

    assert strip_hop_by_hop_headers(headers) == [
        (b"X-End-To-End", b"first"),
        (b"X-End-To-End", b"second"),
    ]


@pytest.mark.asyncio
async def test_request_headers_preserve_repeats_rebuild_host_and_replace_credentials():
    provider = RecordingSecretProvider("upstream-secret")
    settings = LangGraphGatewaySettings(
        upstream_credential_ref="agent.api-key",
        upstream_credential_header="X-Agent-Key",
        upstream_credential_scheme="Token",
    )
    headers = [
        (b"Host", b"gateway.invalid"),
        (b"Authorization", b"Bearer client-secret"),
        (b"X-API-Key", b"client-api-key"),
        (b"X-Agent-Key", b"Token spoofed"),
        (b"X-Trace", b"one"),
        (b"X-Trace", b"two"),
    ]

    forwarded = await prepare_upstream_request_headers(
        headers,
        upstream_url=httpx.URL("https://agent.example:8443/base"),
        settings=settings,
        secret_provider=provider,
        tenant_id="tenant-a",
    )

    assert forwarded == [
        (b"X-Trace", b"one"),
        (b"X-Trace", b"two"),
        (b"host", b"agent.example:8443"),
        (b"X-Agent-Key", b"Token upstream-secret"),
    ]
    assert provider.calls == [("agent.api-key", "tenant-a", None)]


@pytest.mark.asyncio
async def test_request_without_configured_credential_adds_no_authentication_header():
    provider = RecordingSecretProvider("must-not-be-read")

    forwarded = await prepare_upstream_request_headers(
        [(b"Authorization", b"client"), (b"X-API-Key", b"client")],
        upstream_url=httpx.URL("http://agent-server:8123"),
        settings=LangGraphGatewaySettings(),
        secret_provider=provider,
        tenant_id="tenant-a",
    )

    assert forwarded == [(b"host", b"agent-server:8123")]
    assert provider.calls == []


@pytest.mark.asyncio
async def test_missing_configured_credential_fails_with_stable_code():
    provider = RecordingSecretProvider(None)
    settings = LangGraphGatewaySettings(
        deployment_ref="deployment-a",
        upstream_credential_ref="agent.api-key",
    )

    with pytest.raises(UpstreamCredentialUnavailableError) as caught:
        await prepare_upstream_request_headers(
            [],
            upstream_url=httpx.URL("http://agent-server:8123"),
            settings=settings,
            secret_provider=provider,
            tenant_id="tenant-a",
        )

    assert caught.value.code == "zeroth.upstream_credential_unavailable"
    assert "agent.api-key" not in str(caught.value)
    assert provider.calls == [("agent.api-key", "tenant-a", "deployment-a")]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["", "   "])
async def test_empty_configured_credential_is_unavailable(value):
    with pytest.raises(UpstreamCredentialUnavailableError):
        await prepare_upstream_request_headers(
            [],
            upstream_url=httpx.URL("http://agent-server:8123"),
            settings=LangGraphGatewaySettings(upstream_credential_ref="agent.api-key"),
            secret_provider=RecordingSecretProvider(value),
        )
