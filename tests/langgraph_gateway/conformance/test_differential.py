from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from .harness import (
    EXPECTED_GOVERNANCE_ADDITIONS,
    CapturedExchange,
    ConformanceServers,
    capture_response,
    compare_exchanges,
)


pytestmark = pytest.mark.langgraph_conformance


def _capture(**updates: object) -> CapturedExchange:
    values = {
        "status_code": 200,
        "headers": (("content-type", "application/json"),),
        "raw_chunks": (b'{"result":"echo:hello"}',),
        "final_json": {"result": "echo:hello", "run_id": "generated-a"},
        "state": {"values": {"result": "echo:hello"}, "created_at": "generated-time-a"},
        "interrupts": (),
        "resume_values": (),
        "tool_sequence": (),
        "errors": (),
        "cancellation_outcome": None,
        "terminal_state": "success",
        "forwarded_context_present": False,
        "audit_event_present": False,
    }
    values.update(updates)
    return CapturedExchange(**values)


def test_report_separates_only_the_four_declared_governance_additions() -> None:
    direct = _capture()
    proxied = _capture(
        headers=(
            ("content-type", "application/json"),
            ("x-correlation-id", "corr-1"),
            ("x-zeroth-governance-level", "admission"),
        ),
        final_json={"result": "echo:hello", "run_id": "generated-b"},
        state={"values": {"result": "echo:hello"}, "created_at": "generated-time-b"},
        forwarded_context_present=True,
        audit_event_present=True,
    )

    report = compare_exchanges(direct, proxied)

    assert report.semantic_divergences == []
    assert report.expected_governance_additions == list(EXPECTED_GOVERNANCE_ADDITIONS)


def test_comparison_never_reorders_chunks_or_discards_unknown_fields() -> None:
    direct = _capture(raw_chunks=(b"one", b"two"))
    proxied = _capture(raw_chunks=(b"two", b"one"))
    report = compare_exchanges(direct, proxied)
    assert "raw_chunks" in report.semantic_divergences

    direct = _capture(final_json={"result": "ok"})
    proxied = _capture(final_json={"result": "ok", "unknown": True})
    report = compare_exchanges(direct, proxied)
    assert "final_json" in report.semantic_divergences


@pytest.fixture(scope="module")
def servers() -> Iterator[ConformanceServers]:
    with ConformanceServers() as running:
        yield running


@pytest.mark.parametrize(
    "input_payload",
    [
        {"mode": "echo", "text": "differential"},
        {
            "mode": "tools",
            "tool_calls": [
                {"arguments": {"city": "Raleigh"}, "name": "lookup_weather"},
                {"arguments": {"policy_id": "policy-7"}, "name": "lookup_policy"},
            ],
        },
    ],
    ids=["echo", "recorded-tools"],
)
def test_live_direct_and_proxied_wait_cases_have_zero_semantic_divergence(
    servers: ConformanceServers,
    input_payload: dict[str, object],
    tmp_path: Path,
) -> None:
    payload = {"assistant_id": "conformance", "input": input_payload}
    with httpx.Client(timeout=15) as client:
        direct = capture_response(client.post(f"{servers.direct_url}/runs/wait", json=payload))
        proxied = capture_response(client.post(f"{servers.gateway_url}/runs/wait", json=payload))
    report = compare_exchanges(direct, proxied)
    if report.semantic_divergences:
        report.write_human_report(tmp_path / "differential-report.txt")
    assert report.semantic_divergences == []
    assert report.expected_governance_additions == list(EXPECTED_GOVERNANCE_ADDITIONS)


def _interrupt_resume(url: str) -> tuple[dict[str, object], dict[str, object]]:
    with httpx.Client(base_url=url, timeout=15) as client:
        thread = client.post("/threads", json={}).json()
        paused = client.post(
            f"/threads/{thread['thread_id']}/runs/wait",
            json={
                "assistant_id": "conformance",
                "input": {"mode": "interrupt", "text": "approve"},
            },
        )
        paused.raise_for_status()
        resumed = client.post(
            f"/threads/{thread['thread_id']}/runs/wait",
            json={"assistant_id": "conformance", "command": {"resume": "approved"}},
        )
        resumed.raise_for_status()
        return paused.json(), resumed.json()


def test_live_interrupt_and_native_resume_values_match(servers: ConformanceServers) -> None:
    direct_paused, direct_resumed = _interrupt_resume(servers.direct_url)
    proxy_paused, proxy_resumed = _interrupt_resume(servers.gateway_url)
    assert proxy_paused["__interrupt__"][0]["value"] == direct_paused["__interrupt__"][0]["value"]
    assert proxy_resumed == direct_resumed
