from __future__ import annotations

import httpx
import pytest

from zeroth.contracts.graph import HttpRequestNode, HttpRequestNodeData
from zeroth.integrations.http.client import ResilientHttpClient
from zeroth.integrations.http.errors import HttpRetryExhaustedError
from zeroth.governance.audit.capture_projection import ContentFreeProjection
from zeroth.governance.audit.capture_scrub import RedactionChain
from zeroth.platform.config.models import HttpClientSettings
from zeroth.runtime.orchestration import NodeDispatcher, RuntimeToolExecutor
from zeroth.runtime.runs import Run


class _UnusedRunner:
    pass


def _dispatcher(client: ResilientHttpClient | None) -> NodeDispatcher:
    runner = _UnusedRunner()
    return NodeDispatcher(
        agent_runners={},
        executable_unit_runner=runner,
        tool_executor=RuntimeToolExecutor(executable_unit_runner=runner),
        http_client=client,
    )


def _node(**overrides: object) -> HttpRequestNode:
    values: dict[str, object] = {"url": "http://127.0.0.1:8787/data"}
    values.update(overrides)
    return HttpRequestNode(
        node_id="fetch",
        graph_version_ref="http-demo:v1",
        http_request=HttpRequestNodeData(**values),  # type: ignore[arg-type]
    )


def _run() -> Run:
    return Run(graph_version_ref="http-demo:v1", deployment_ref="http-demo-v1")


@pytest.mark.asyncio
async def test_http_node_fails_closed_without_a_runtime_client() -> None:
    with pytest.raises(Exception, match="requires the resilient HTTP client"):
        await _dispatcher(None).dispatch_inner(_node(), _run(), {})


@pytest.mark.asyncio
async def test_http_node_returns_body_and_sanitized_zero_cost_audit() -> None:
    client = ResilientHttpClient(HttpClientSettings())
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={"state": "ready"},
                headers={"Authorization": "never-audit-this"},
            )
        )
    )

    output, audit = await _dispatcher(client).dispatch_inner(_node(), _run(), {})

    assert output["http_response"] == {
        "status_code": 200,
        "content_type": "application/json",
        "body": {"state": "ready"},
    }
    assert audit["execution_mode"] == "resilient_http_get"
    assert audit["http"]["url"] == "http://127.0.0.1:8787/data"
    assert "Authorization" not in str(audit)
    assert audit["cost_usd"] == 0.0
    assert audit["estimated_cost_usd"] == 0.0
    assert audit["provider_call_count"] == 0
    projected, summary = ContentFreeProjection(RedactionChain().scrub).metadata(audit)
    assert projected["node_kind"] == "http_request"
    assert projected["upstream_status_code"] == 200
    assert projected["retry_count"] == 0
    assert projected["cost_usd"] == 0.0
    assert projected["target_url_sha256"] == audit["target_url_sha256"]
    assert "http" not in projected
    assert summary["dropped_keys"] >= 1
    await client.aclose()


@pytest.mark.asyncio
async def test_http_node_attaches_sanitized_call_record_to_failure() -> None:
    client = ResilientHttpClient(
        HttpClientSettings(max_retries=0, retry_backoff_base=0, retry_max_delay=0)
    )
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(503))
    )

    with pytest.raises(HttpRetryExhaustedError) as caught:
        await _dispatcher(client).dispatch_inner(_node(), _run(), {})

    audit = caught.value.audit_record
    assert audit["http"]["status_code"] is None
    assert audit["http"]["error"] == "http_retry_exhausted_error"
    assert audit["cost_usd"] == 0.0
    assert audit["provider_call_count"] == 0
    await client.aclose()


@pytest.mark.asyncio
async def test_http_node_refuses_an_oversized_response_without_auditing_the_body() -> None:
    client = ResilientHttpClient(HttpClientSettings())
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"too large"))
    )

    with pytest.raises(Exception, match="response exceeded 4 bytes") as caught:
        await _dispatcher(client).dispatch_inner(
            _node(max_response_bytes=4),
            _run(),
            {},
        )

    assert "too large" not in str(caught.value.audit_record)
    await client.aclose()
