from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from release.acceptance.config import AcceptanceConfig
from release.acceptance.transport import AcceptanceTransport, TransportError, redact


def _resolved(tmp_path: Path):
    identity = tmp_path / "identity.json"
    identity.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "commit": "a" * 40,
                "package": {"version": "1", "artifacts": {}},
                "image": {"candidate": "sha256:" + "b" * 64},
            }
        ),
        encoding="utf-8",
    )
    return AcceptanceConfig.model_validate(
        {
            "schema_version": 1,
            "base_url": "https://candidate.example",
            "tenant_id": "acceptance-tenant",
            "deployment_ref": "dep",
            "candidate_identity": str(identity),
            "credentials": {"operator": "OP", "reviewer": "REV", "admin": "ADM"},
            "lifecycle": {"restart_url": "/restart", "shutdown_url": "/shutdown"},
        }
    ).resolve({"OP": "operator-secret", "REV": "reviewer-secret", "ADM": "admin-secret"})


@pytest.mark.asyncio
async def test_http_requests_are_same_origin_tenant_bound_and_role_authenticated(
    tmp_path: Path,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True}, headers={"X-Correlation-ID": "corr-1"})

    config = _resolved(tmp_path)
    async with AcceptanceTransport(config, http_transport=httpx.MockTransport(handler)) as client:
        observation = await client.request("operator", "GET", "/health/ready")

        with pytest.raises(TransportError, match="origin-relative"):
            await client.request("operator", "GET", "https://evil.example/steal")

    assert str(seen[0].url) == "https://candidate.example/health/ready"
    assert seen[0].headers["X-API-Key"] == "operator-secret"
    assert seen[0].headers["X-Acceptance-Tenant"] == "acceptance-tenant"
    assert observation.correlation_id == "corr-1"


@pytest.mark.asyncio
async def test_redirects_and_oversized_responses_fail_closed(tmp_path: Path) -> None:
    responses = iter(
        [
            httpx.Response(302, headers={"location": "https://evil.example/steal"}),
            httpx.Response(200, content=b"x" * 33),
        ]
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return next(responses)

    async with AcceptanceTransport(
        _resolved(tmp_path), http_transport=httpx.MockTransport(handler), max_response_bytes=32
    ) as client:
        with pytest.raises(TransportError, match="redirect"):
            await client.request("operator", "GET", "/redirect")
        with pytest.raises(TransportError, match="exceeds"):
            await client.request("operator", "GET", "/large")


def test_redaction_removes_secrets_from_nested_diagnostics() -> None:
    value = {
        "message": "Bearer admin-secret",
        "nested": ["operator-secret", {"value": "safe"}],
    }

    redacted = redact(
        value,
        {"admin": SecretStr("admin-secret"), "operator": SecretStr("operator-secret")},
    )

    serialized = json.dumps(redacted)
    assert "admin-secret" not in serialized
    assert "operator-secret" not in serialized
    assert serialized.count("[REDACTED]") == 2


@pytest.mark.asyncio
async def test_websocket_uses_ws_origin_auth_and_tenant(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def send(self, value: str) -> None:
            captured["sent"] = value

        def __aiter__(self):
            self._messages = iter(['{"event":"start"}', '{"event":"end"}'])
            return self

        async def __anext__(self) -> str:
            try:
                return next(self._messages)
            except StopIteration as error:
                raise StopAsyncIteration from error

    def connect(url: str, **kwargs: object):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return Socket()

    async with AcceptanceTransport(_resolved(tmp_path), websocket_connect=connect) as client:
        events = await client.websocket_events(
            "reviewer", "/runs/stream", {"run_id": "run-1"}, max_events=2
        )

    assert captured["url"] == "wss://candidate.example/runs/stream"
    headers = dict(captured["kwargs"]["additional_headers"])
    assert headers["X-API-Key"] == "reviewer-secret"
    assert headers["X-Acceptance-Tenant"] == "acceptance-tenant"
    assert events == [{"event": "start"}, {"event": "end"}]
