from __future__ import annotations

import asyncio

import httpx
import pytest

from zeroth.governance.policy.models import Capability
from zeroth.integrations.http.client import ResilientHttpClient
from zeroth.integrations.http.errors import CircuitOpenError, HttpRetryExhaustedError
from zeroth.platform.config.models import HttpClientSettings


@pytest.mark.asyncio
async def test_request_with_record_returns_its_own_record_under_concurrency() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("slow"):
            await asyncio.sleep(0.01)
        return httpx.Response(200, json={"path": request.url.path})

    client = ResilientHttpClient(HttpClientSettings())
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    slow, fast = await asyncio.gather(
        client.request_with_record("GET", "http://127.0.0.1/slow"),
        client.request_with_record("GET", "http://127.0.0.1/fast"),
    )

    assert slow.call_record.url == "http://127.0.0.1/slow"
    assert fast.call_record.url == "http://127.0.0.1/fast"
    assert slow.response.json() == {"path": "/slow"}
    assert fast.response.json() == {"path": "/fast"}
    await client.aclose()


@pytest.mark.asyncio
async def test_retry_exhaustion_carries_the_invocation_record() -> None:
    client = ResilientHttpClient(
        HttpClientSettings(max_retries=1, retry_backoff_base=0, retry_max_delay=0)
    )
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(503))
    )

    with pytest.raises(HttpRetryExhaustedError) as caught:
        await client.request_with_record("GET", "http://127.0.0.1/fail")

    record = caught.value.http_call_record
    assert record.url == "http://127.0.0.1/fail"
    assert record.retry_count == 1
    assert record.error == "HTTP 503"
    await client.aclose()


@pytest.mark.asyncio
async def test_circuit_open_carries_a_sanitized_invocation_record() -> None:
    client = ResilientHttpClient(
        HttpClientSettings(
            max_retries=0,
            retry_backoff_base=0,
            retry_max_delay=0,
            circuit_breaker_threshold=1,
            circuit_breaker_reset_timeout=60,
        )
    )
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(503))
    )
    with pytest.raises(HttpRetryExhaustedError):
        await client.request_with_record("GET", "http://127.0.0.1/data")

    with pytest.raises(CircuitOpenError) as caught:
        await client.request_with_record("GET", "http://127.0.0.1/data")

    record = caught.value.http_call_record
    assert record.url == "http://127.0.0.1/data"
    assert record.circuit_breaker_state == "open"
    assert record.error == "circuit_open"
    await client.aclose()


@pytest.mark.asyncio
async def test_admission_failure_still_carries_its_invocation_record() -> None:
    client = ResilientHttpClient(HttpClientSettings())

    with pytest.raises(Exception, match="Missing required capability") as caught:
        await client.request_with_record(
            "GET",
            "http://127.0.0.1/data",
            effective_capabilities={Capability.EXTERNAL_API_CALL},
        )

    assert caught.value.http_call_record.model_dump() == {
        "url": "http://127.0.0.1/data",
        "method": "GET",
        "status_code": None,
        "latency_ms": caught.value.http_call_record.latency_ms,
        "response_size_bytes": None,
        "retry_count": 0,
        "circuit_breaker_state": None,
        "error": "HttpClientError",
    }
    await client.aclose()
