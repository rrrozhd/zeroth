from __future__ import annotations

import httpx
import pytest

from release.live_evaluation.resilient_http_scenario_server import (
    HttpScenarioState,
    create_http_scenario_app,
    validate_loopback_bind_host,
)


@pytest.mark.asyncio
async def test_health_is_ready_without_mutating_scenario_state() -> None:
    state = HttpScenarioState(retry_failures=2, timeout_seconds=0.01)
    transport = httpx.ASGITransport(app=create_http_scenario_app(state))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        response = await client.get("/health")
        snapshot = (await client.get("/control/events")).json()

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert snapshot == {"events": [], "recovered": False}


@pytest.mark.asyncio
async def test_retry_scenario_fails_twice_then_recovers_with_sanitized_events() -> None:
    state = HttpScenarioState(retry_failures=2, timeout_seconds=0.01)
    transport = httpx.ASGITransport(app=create_http_scenario_app(state))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        statuses = [
            (await client.get("/scenario/retry-then-success")).status_code
            for _ in range(3)
        ]
        events = (await client.get("/control/events")).json()["events"]

    assert statuses == [503, 503, 200]
    assert [event["status_code"] for event in events] == [503, 503, 200]
    assert all(set(event) == {"sequence", "scenario", "status_code"} for event in events)


@pytest.mark.asyncio
async def test_circuit_peer_can_be_switched_to_recovery_and_reset() -> None:
    state = HttpScenarioState(retry_failures=1, timeout_seconds=0.01)
    transport = httpx.ASGITransport(app=create_http_scenario_app(state))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        assert (await client.get("/scenario/circuit")).status_code == 503
        assert (await client.post("/control/recover")).status_code == 204
        assert (await client.get("/scenario/circuit")).status_code == 200
        assert (await client.post("/control/reset")).status_code == 204
        snapshot = (await client.get("/control/events")).json()

    assert snapshot == {"events": [], "recovered": False}


def test_server_refuses_non_loopback_bind_hosts() -> None:
    assert validate_loopback_bind_host("127.0.0.1") == "127.0.0.1"
    assert validate_loopback_bind_host("::1") == "::1"
    with pytest.raises(ValueError, match="loopback"):
        validate_loopback_bind_host("0.0.0.0")
