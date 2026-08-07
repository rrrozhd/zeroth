from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from .harness import (
    EXPECTED_GOVERNANCE_ADDITIONS,
    CapturedExchange,
    ConformanceServers,
    GeneratedValueMap,
    capture_response,
    compare_exchanges,
    execute_paired_case,
)
from .cases import CASES, ConformanceCase


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

    generated = GeneratedValueMap.from_pairs(
        [("generated-a", "generated-b"), ("generated-time-a", "generated-time-b")]
    )
    report = compare_exchanges(direct, proxied, generated_values=generated)

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


@pytest.mark.parametrize(
    ("direct_updates", "proxied_updates", "expected_field", "expected_path"),
    [
        (
            {"retry_attempts": (1, 2)},
            {"retry_attempts": (1,)},
            "retry_attempts",
            "#/retry_attempts/1",
        ),
        (
            {"disconnect_outcome": "client_stream_closed"},
            {"disconnect_outcome": None},
            "disconnect_outcome",
            "#/disconnect_outcome",
        ),
    ],
)
def test_comparator_rejects_retry_or_disconnect_mutations(
    direct_updates: dict[str, object],
    proxied_updates: dict[str, object],
    expected_field: str,
    expected_path: str,
) -> None:
    report = compare_exchanges(_capture(**direct_updates), _capture(**proxied_updates))

    assert expected_field in report.semantic_divergences
    assert report.first_divergence_path == expected_path


@pytest.mark.parametrize(
    ("direct_updates", "proxied_updates", "expected_path"),
    [
        pytest.param(
            {
                "tool_sequence": (
                    {"name": "lookup_weather", "arguments": {"city": "Raleigh"}},
                    {"name": "lookup_policy", "arguments": {"policy_id": "policy-7"}},
                )
            },
            {
                "tool_sequence": (
                    {"name": "lookup_policy", "arguments": {"policy_id": "policy-7"}},
                    {"name": "lookup_weather", "arguments": {"city": "Raleigh"}},
                )
            },
            "#/tool_sequence/0/arguments/city",
            id="tool-order",
        ),
        pytest.param(
            {
                "tool_sequence": (
                    {"name": "lookup_weather", "arguments": {"city": "Raleigh"}},
                )
            },
            {
                "tool_sequence": (
                    {"name": "lookup_weather", "arguments": {"city": "Durham"}},
                )
            },
            "#/tool_sequence/0/arguments/city",
            id="tool-arguments",
        ),
        pytest.param(
            {"state": {"values": {"result": "direct"}}},
            {"state": {"values": {"result": "proxied"}}},
            "#/state/values/result",
            id="final-state",
        ),
        pytest.param({}, {}, None, id="identical"),
    ],
)
def test_report_identifies_the_exact_first_divergence_path(
    direct_updates: dict[str, object],
    proxied_updates: dict[str, object],
    expected_path: str | None,
) -> None:
    report = compare_exchanges(_capture(**direct_updates), _capture(**proxied_updates))

    assert report.first_divergence_path == expected_path


def test_human_report_names_the_path_without_leaking_divergent_values(tmp_path: Path) -> None:
    report = compare_exchanges(
        _capture(state={"values": {"result": "direct-secret"}}),
        _capture(state={"values": {"result": "proxied-secret"}}),
    )
    path = tmp_path / "differential-report.txt"

    report.write_human_report(path)

    text = path.read_text(encoding="utf-8")
    assert "first divergence: #/state/values/result" in text
    assert "direct-secret" not in text
    assert "proxied-secret" not in text


@pytest.mark.parametrize(
    ("key", "expected_path"),
    [
        ("a/b~c", "#/final_json/a~1b~0c"),
        ("line\nbreak", "#/final_json/line%0Abreak"),
    ],
)
def test_divergence_path_sorts_and_safely_escapes_mapping_keys(
    key: str,
    expected_path: str,
    tmp_path: Path,
) -> None:
    direct = _capture(
        raw_chunks=(b"same", b"body"),
        final_json={"z": "direct-z", key: "direct"},
    )
    proxied = _capture(
        raw_chunks=(b"same", b"body"),
        final_json={key: "proxied", "z": "proxied-z"},
    )

    report = compare_exchanges(direct, proxied)
    path = tmp_path / "differential-report.txt"
    report.write_human_report(path)
    text = path.read_text(encoding="utf-8")

    assert report.first_divergence_path == expected_path
    assert f"first divergence: {expected_path}\n" in text
    assert key not in text
    assert "direct-z" not in text
    assert "proxied-z" not in text


@pytest.mark.parametrize(
    "proxied_frames",
    [
        (b"event: values\ndata: one\n\n",),
        (b"event: custom\ndata: two\n\n", b"event: values\ndata: one\n\n"),
        (b"event: values\ndata: one\r\n\r\n", b"event: custom\ndata: two\n\n"),
    ],
    ids=["missing", "reordered", "altered-framing"],
)
def test_sse_frame_comparison_rejects_missing_reordered_or_reframed_bytes(
    proxied_frames: tuple[bytes, ...],
) -> None:
    direct = _capture(
        raw_chunks=(
            b"event: values\ndata: one\n\n",
            b"event: custom\ndata: two\n\n",
        ),
        final_json=None,
    )
    proxied = _capture(raw_chunks=proxied_frames, final_json=None)

    assert "raw_chunks" in compare_exchanges(direct, proxied).semantic_divergences


def test_normalization_requires_an_explicit_pair_mapping() -> None:
    direct = _capture(final_json={"assistant_id": "assistant-direct"})
    proxied = _capture(final_json={"assistant_id": "assistant-proxy"})
    assert "final_json" in compare_exchanges(direct, proxied).semantic_divergences

    arbitrary_direct = "value-11111111-1111-1111-1111-111111111111"
    arbitrary_proxy = "value-22222222-2222-2222-2222-222222222222"
    direct = _capture(final_json={"payload": arbitrary_direct})
    proxied = _capture(final_json={"payload": arbitrary_proxy})
    mapping = GeneratedValueMap.from_pairs([("known-direct", "known-proxy")])
    assert (
        "final_json"
        in compare_exchanges(direct, proxied, generated_values=mapping).semantic_divergences
    )


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
        {
            "mode": "model",
            "model_request": {
                "messages": [{"content": "fixture question", "role": "user"}],
                "model": "fixture-model-v1",
                "temperature": 0,
            },
        },
        {"mode": "retry", "retry_key": "paired-retry"},
    ],
    ids=["echo", "recorded-tools", "recorded-model", "retry-once"],
)
def test_live_direct_and_proxied_wait_cases_have_zero_semantic_divergence(
    servers: ConformanceServers,
    input_payload: dict[str, object],
    tmp_path: Path,
) -> None:
    payload = {"assistant_id": "conformance", "input": input_payload}
    with httpx.Client(timeout=15) as client:
        direct = capture_response(client.post(f"{servers.direct_url}/runs/wait", json=payload))
        evidence_start = len(servers.evidence())
        proxied_response = client.post(
            f"{servers.gateway_url}/runs/wait",
            json=payload,
            headers={"x-api-key": "gateway-key"},
        )
        evidence = servers.evidence(since=evidence_start)
        proxied = capture_response(
            proxied_response,
            forwarded_context_present=any(
                row.get("kind") == "forwarded" and row.get("zeroth_present") for row in evidence
            ),
            audit_event_present=any(row.get("kind") == "audit" for row in evidence),
        )
    assert dict(proxied.headers)["x-zeroth-governance-level"] == "observed"
    assert proxied.audit_event_present is True
    if input_payload["mode"] == "model":
        expected = {
            "content": "fixture response",
            "finish_reason": "stop",
            "role": "assistant",
        }
        assert direct.final_json["model_response"] == expected
        assert proxied.final_json["model_response"] == expected
    if input_payload["mode"] == "retry":
        assert direct.retry_attempts == (1, 2)
        assert proxied.retry_attempts == (1, 2)
    direct_headers = dict(direct.headers)
    proxied_headers = dict(proxied.headers)
    header_pairs = []
    for header in ("date", "location", "content-location"):
        if header in direct_headers and header in proxied_headers:
            header_pairs.append((direct_headers[header], proxied_headers[header]))
    report = compare_exchanges(
        direct,
        proxied,
        generated_values=GeneratedValueMap.from_pairs(header_pairs),
    )
    if report.semantic_divergences:
        report.write_human_report(tmp_path / "differential-report.txt")
    assert report.semantic_divergences == []
    assert report.expected_governance_additions == list(EXPECTED_GOVERNANCE_ADDITIONS)


@pytest.mark.parametrize("case", CASES, ids=lambda case: f"paired-{case.name}")
def test_every_manifest_case_runs_direct_and_through_the_real_gateway(
    servers: ConformanceServers,
    case: ConformanceCase,
    tmp_path: Path,
) -> None:
    result = execute_paired_case(servers, case)
    assert result.direct.status_code in case.expected_status
    assert result.proxied.status_code in case.gateway_expected_status

    if case.group in {"auth", "unsupported"}:
        assert result.proxied.status_code in case.gateway_expected_status
        return

    report = compare_exchanges(
        result.direct,
        result.proxied,
        generated_values=result.generated_values,
    )
    if report.semantic_divergences:
        report.write_human_report(tmp_path / f"{case.name}-differential-report.txt")
    assert report.semantic_divergences == []
    assert set(report.expected_governance_additions) <= set(EXPECTED_GOVERNANCE_ADDITIONS)
    if case.governance == "governed":
        assert report.expected_governance_additions == list(EXPECTED_GOVERNANCE_ADDITIONS)


@pytest.mark.parametrize("case_name", ["thread-stream", "protocol-event-stream"])
def test_paired_subscriptions_capture_bounded_exact_sse_frames(
    servers: ConformanceServers,
    case_name: str,
) -> None:
    case = next(case for case in CASES if case.name == case_name)
    result = execute_paired_case(servers, case)

    assert result.direct.raw_chunks
    assert result.proxied.raw_chunks
    assert len(result.direct.raw_chunks) <= 12
    assert len(result.proxied.raw_chunks) <= 12
    assert all(frame.endswith((b"\n\n", b"\r\n\r\n")) for frame in result.direct.raw_chunks)
    assert all(frame.endswith((b"\n\n", b"\r\n\r\n")) for frame in result.proxied.raw_chunks)
    assert result.direct.disconnect_outcome == "client_stream_closed"
    assert result.proxied.disconnect_outcome == "client_stream_closed"
    report = compare_exchanges(
        result.direct,
        result.proxied,
        generated_values=result.generated_values,
    )
    assert report.semantic_divergences == []


def _interrupt_resume(
    url: str, *, headers: dict[str, str] | None = None
) -> tuple[dict[str, object], dict[str, object]]:
    with httpx.Client(base_url=url, timeout=15) as client:
        thread = client.post("/threads", json={}, headers=headers).json()
        paused = client.post(
            f"/threads/{thread['thread_id']}/runs/wait",
            json={
                "assistant_id": "conformance",
                "input": {"mode": "interrupt", "text": "approve"},
            },
            headers=headers,
        )
        paused.raise_for_status()
        resumed = client.post(
            f"/threads/{thread['thread_id']}/runs/wait",
            json={"assistant_id": "conformance", "command": {"resume": "approved"}},
            headers=headers,
        )
        resumed.raise_for_status()
        return paused.json(), resumed.json()


def test_live_interrupt_and_native_resume_values_match(servers: ConformanceServers) -> None:
    direct_paused, direct_resumed = _interrupt_resume(servers.direct_url)
    proxy_paused, proxy_resumed = _interrupt_resume(
        servers.gateway_url, headers={"x-api-key": "gateway-key"}
    )
    assert proxy_paused["__interrupt__"][0]["value"] == direct_paused["__interrupt__"][0]["value"]
    assert proxy_resumed == direct_resumed
