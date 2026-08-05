from __future__ import annotations

import asyncio
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from zeroth.core.langgraph_gateway.capabilities import (
    CapabilityReporter,
    NoCapabilityEvidenceProvider,
)
from zeroth.core.langgraph_gateway.compatibility import (
    CompatibilityDetector,
    EXPECTED_AGENT_SERVER_OPENAPI_FINGERPRINTS,
    fingerprint_openapi,
)
from zeroth.core.langgraph_gateway.models import (
    CompatibilityStatus,
    GovernanceLevel,
    RunCapabilityEvidence,
)


FIXTURE = Path(__file__).parent / "fixtures" / "openapi-0.11.1.operations.json"


def _openapi_document() -> dict[str, object]:
    projection = json.loads(FIXTURE.read_text())
    paths: dict[str, dict[str, dict[str, str]]] = {}
    for method, path, operation_id in projection["operations"]:
        paths.setdefault(path, {})[method.lower()] = {"operationId": operation_id}
    return {"openapi": "3.1.0", "info": {"description": "ignored"}, "paths": paths}


def _transport(
    *,
    info: object = None,
    openapi: object = None,
    openapi_status: int = 200,
) -> tuple[httpx.MockTransport, list[str]]:
    calls: list[str] = []
    info = {"langgraph_api_version": "0.11.1"} if info is None else info
    openapi = _openapi_document() if openapi is None else openapi

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/info":
            return httpx.Response(200, json=info)
        if request.url.path == "/ok":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/openapi.json":
            return httpx.Response(openapi_status, json=openapi)
        return httpx.Response(404)

    return httpx.MockTransport(handler), calls


async def _detect(transport: httpx.MockTransport):
    async with httpx.AsyncClient(transport=transport, base_url="https://upstream.test") as client:
        return await CompatibilityDetector(client, timeout_seconds=0.1).detect()


async def _detect_asgi(app, **detector_options: object):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://upstream.test"
    ) as client:
        return await CompatibilityDetector(client, **detector_options).detect()


class TrackedByteStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], *, delay_seconds: float = 0.0) -> None:
        self.chunks = chunks
        self.delay_seconds = delay_seconds
        self.yielded = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.delay_seconds:
                await asyncio.sleep(self.delay_seconds)
            self.yielded += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class ProbeStreamTransport(httpx.AsyncBaseTransport):
    def __init__(self, streams: dict[str, TrackedByteStream]) -> None:
        self.streams = streams

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=self.streams[request.url.path], request=request)


@pytest.mark.asyncio
async def test_exact_version_and_expected_openapi_fingerprint_are_supported() -> None:
    transport, calls = _transport()

    result = await _detect(transport)

    assert calls == ["/info", "/ok", "/openapi.json"]
    assert result.tested_langgraph_versions == ("1.2.9",)
    assert result.tested_agent_server_versions == ("0.11.1",)
    assert result.detected_agent_server_version == "0.11.1"
    assert result.openapi_fingerprint == EXPECTED_AGENT_SERVER_OPENAPI_FINGERPRINTS["0.11.1"]
    assert result.status is CompatibilityStatus.SUPPORTED


@pytest.mark.asyncio
async def test_exact_version_alone_is_supported_when_openapi_is_not_exposed() -> None:
    transport, _ = _transport(openapi_status=404)

    result = await _detect(transport)

    assert result.status is CompatibilityStatus.SUPPORTED
    assert result.openapi_fingerprint is None


@pytest.mark.asyncio
async def test_managed_server_without_version_uses_known_fingerprint() -> None:
    transport, _ = _transport(info={})

    result = await _detect(transport)

    assert result.detected_agent_server_version is None
    assert result.status is CompatibilityStatus.SUPPORTED


@pytest.mark.asyncio
async def test_unknown_patch_is_unsupported_even_with_known_shape() -> None:
    transport, _ = _transport(info={"langgraph_api_version": "0.11.2"})

    result = await _detect(transport)

    assert result.status is CompatibilityStatus.UNSUPPORTED
    assert result.reason == "detected Agent Server version is not in the tested matrix"


@pytest.mark.asyncio
async def test_malformed_info_is_unsupported() -> None:
    transport, _ = _transport(info=["not", "an", "object"])

    result = await _detect(transport)

    assert result.status is CompatibilityStatus.UNSUPPORTED
    assert result.reason == "upstream /info response is malformed"


@pytest.mark.asyncio
async def test_deeply_nested_info_json_is_safely_malformed() -> None:
    nested = (b"[" * 10_000) + b"0" + (b"]" * 10_000)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(200, content=nested)
        if request.url.path == "/ok":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    result = await _detect(httpx.MockTransport(handler))

    assert result.status is CompatibilityStatus.UNSUPPORTED
    assert result.reason == "upstream /info response is malformed"


@pytest.mark.asyncio
async def test_changed_openapi_shape_is_unsupported() -> None:
    document = _openapi_document()
    document["paths"]["/threads"]["post"]["operationId"] = "changed_operation"  # type: ignore[index]
    transport, _ = _transport(openapi=document)

    result = await _detect(transport)

    assert result.status is CompatibilityStatus.UNSUPPORTED
    assert result.reason == "upstream version and OpenAPI evidence conflict"


@pytest.mark.asyncio
async def test_deeply_nested_openapi_json_is_safely_malformed() -> None:
    nested = (b"[" * 10_000) + b"0" + (b"]" * 10_000)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(200, json={"langgraph_api_version": "0.11.1"})
        if request.url.path == "/ok":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, content=nested)

    result = await _detect(httpx.MockTransport(handler))

    assert result.status is CompatibilityStatus.UNSUPPORTED
    assert result.reason == "upstream OpenAPI response is malformed"


@pytest.mark.asyncio
async def test_outage_is_unavailable_without_retries() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("secret internal host detail", request=request)

    result = await _detect(httpx.MockTransport(handler))

    assert calls == 1
    assert result.status is CompatibilityStatus.UNAVAILABLE
    assert result.reason == "upstream Agent Server is unavailable"
    assert "secret" not in result.reason


@pytest.mark.parametrize("timeout", [0.0, -1.0, math.nan, math.inf, -math.inf])
def test_detector_rejects_nonpositive_or_nonfinite_timeout(timeout: float) -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        CompatibilityDetector(object(), timeout_seconds=timeout)  # type: ignore[arg-type]


@pytest.mark.parametrize("limit", [True, 1.5])
def test_detector_requires_integer_response_limits(limit: object) -> None:
    with pytest.raises(ValueError, match="response byte limits"):
        CompatibilityDetector(  # type: ignore[arg-type]
            object(), info_max_response_bytes=limit
        )


@pytest.mark.asyncio
async def test_probes_disable_content_encoding() -> None:
    transport, _ = _transport()
    encodings: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        encodings.append(request.headers.get("accept-encoding"))
        return transport.handle_request(request)

    result = await _detect(httpx.MockTransport(handler))

    assert result.status is CompatibilityStatus.SUPPORTED
    assert encodings == ["identity", "identity", "identity"]


@pytest.mark.asyncio
async def test_streamed_oversize_openapi_is_rejected_and_closed() -> None:
    stream_finished = asyncio.Event()

    async def app(scope, receive, send) -> None:
        path = scope["path"]
        await send({"type": "http.response.start", "status": 200, "headers": []})
        if path == "/info":
            await send(
                {
                    "type": "http.response.body",
                    "body": b'{"langgraph_api_version":"0.11.1"}',
                }
            )
        elif path == "/ok":
            await send({"type": "http.response.body", "body": b'{"ok":true}'})
        else:
            try:
                await send({"type": "http.response.body", "body": b"{" + (b"x" * 128)})
            finally:
                stream_finished.set()

    result = await _detect_asgi(app, timeout_seconds=0.2, openapi_max_response_bytes=32)

    assert result.status is CompatibilityStatus.UNSUPPORTED
    assert result.reason == "upstream OpenAPI response exceeds the probe size limit"
    assert stream_finished.is_set()


@pytest.mark.asyncio
async def test_oversize_probe_stops_incremental_stream_and_closes_it() -> None:
    openapi_stream = TrackedByteStream([b"x" * 10] * 1_000)
    streams = {
        "/info": TrackedByteStream([b'{"langgraph_api_version":"0.11.1"}']),
        "/ok": TrackedByteStream([b'{"ok":true}']),
        "/openapi.json": openapi_stream,
    }
    async with httpx.AsyncClient(
        transport=ProbeStreamTransport(streams), base_url="https://upstream.test"
    ) as client:
        result = await CompatibilityDetector(
            client, timeout_seconds=0.2, openapi_max_response_bytes=15
        ).detect()

    assert result.status is CompatibilityStatus.UNSUPPORTED
    assert openapi_stream.yielded == 2
    assert openapi_stream.closed is True


@pytest.mark.asyncio
async def test_slow_streaming_probe_obeys_wall_clock_deadline_and_cleans_up() -> None:
    stream_cancelled = asyncio.Event()

    async def app(scope, receive, send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        try:
            while True:
                await send({"type": "http.response.body", "body": b" ", "more_body": True})
                await asyncio.sleep(0.02)
        finally:
            stream_cancelled.set()

    started = asyncio.get_running_loop().time()
    try:
        result = await asyncio.wait_for(_detect_asgi(app, timeout_seconds=0.05), timeout=0.2)
    except TimeoutError:
        pytest.fail("compatibility probe exceeded its wall-clock deadline")
    elapsed = asyncio.get_running_loop().time() - started

    assert result.status is CompatibilityStatus.UNAVAILABLE
    assert result.reason == "upstream Agent Server probe timed out"
    assert elapsed < 0.2
    assert stream_cancelled.is_set()


@pytest.mark.asyncio
async def test_timeout_stops_incremental_stream_and_closes_it() -> None:
    info_stream = TrackedByteStream([b" "] * 1_000, delay_seconds=0.02)
    streams = {
        "/info": info_stream,
        "/ok": TrackedByteStream([b'{"ok":true}']),
        "/openapi.json": TrackedByteStream([b"{}"]),
    }
    async with httpx.AsyncClient(
        transport=ProbeStreamTransport(streams), base_url="https://upstream.test"
    ) as client:
        result = await CompatibilityDetector(client, timeout_seconds=0.05).detect()

    assert result.status is CompatibilityStatus.UNAVAILABLE
    assert info_stream.yielded < len(info_stream.chunks)
    assert info_stream.closed is True


@pytest.mark.asyncio
async def test_readiness_redirect_is_not_healthy() -> None:
    transport, _ = _transport()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ok":
            return httpx.Response(302, headers={"location": "/login"})
        return transport.handle_request(request)

    result = await _detect(httpx.MockTransport(handler))

    assert result.status is CompatibilityStatus.UNAVAILABLE
    assert result.reason == "upstream Agent Server readiness probe failed"


@pytest.mark.asyncio
async def test_oversize_readiness_response_is_unavailable() -> None:
    transport, _ = _transport()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ok":
            return httpx.Response(200, content=b"x" * 32)
        return transport.handle_request(request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://upstream.test"
    ) as client:
        result = await CompatibilityDetector(
            client, timeout_seconds=0.1, ok_max_response_bytes=16
        ).detect()

    assert result.status is CompatibilityStatus.UNAVAILABLE
    assert result.reason == "upstream Agent Server readiness response exceeds the probe size limit"


@pytest.mark.asyncio
async def test_version_is_bounded_and_never_reflected_in_reason() -> None:
    hostile = "0.11.2\nsecret=" + ("x" * 1_000)
    transport, _ = _transport(info={"langgraph_api_version": hostile})

    result = await _detect(transport)

    assert result.status is CompatibilityStatus.UNSUPPORTED
    assert result.detected_agent_server_version is None
    assert result.reason == "upstream version and OpenAPI evidence conflict"
    assert "secret" not in result.reason


@pytest.mark.asyncio
async def test_detected_version_requires_its_own_expected_fingerprint() -> None:
    version_one_document = _openapi_document()
    version_two_document = _openapi_document()
    version_two_document["paths"]["/threads"]["post"]["operationId"] = "v2"  # type: ignore[index]
    expected = {
        "1.0.0": fingerprint_openapi(version_one_document),
        "2.0.0": fingerprint_openapi(version_two_document),
    }
    transport, _ = _transport(info={"langgraph_api_version": "1.0.0"}, openapi=version_two_document)
    async with httpx.AsyncClient(transport=transport, base_url="https://upstream.test") as client:
        result = await CompatibilityDetector(
            client,
            tested_agent_server_versions=("1.0.0", "2.0.0"),
            expected_openapi_fingerprints=expected,
            timeout_seconds=0.1,
        ).detect()

    assert result.status is CompatibilityStatus.UNSUPPORTED
    assert result.reason == "upstream version and OpenAPI evidence conflict"


def test_openapi_fingerprint_ignores_descriptions_examples_and_input_order() -> None:
    left = _openapi_document()
    right = _openapi_document()
    right["info"] = {"description": "different"}
    right["paths"] = dict(reversed(list(right["paths"].items())))  # type: ignore[union-attr]
    first_path = next(iter(right["paths"].values()))  # type: ignore[union-attr]
    first_operation = next(iter(first_path.values()))
    first_operation["examples"] = {"ignored": {"value": "secret"}}

    assert fingerprint_openapi(left) == fingerprint_openapi(right)


class StaticEvidenceProvider:
    def __init__(self, evidence: RunCapabilityEvidence | None) -> None:
        self.evidence = evidence

    async def evidence_for_run(self, correlation_id: str) -> RunCapabilityEvidence | None:
        return self.evidence

    async def evidence_for_governance_run(
        self, governance_run_id: str
    ) -> RunCapabilityEvidence | None:
        return self.evidence


class FalsyEvidenceProvider(StaticEvidenceProvider):
    def __bool__(self) -> bool:
        return False


class RaisingEvidenceProvider:
    async def evidence_for_run(self, correlation_id: str) -> RunCapabilityEvidence | None:
        raise ValueError("malformed backend evidence containing secret")


class RaisingEvidence:
    @property
    def signature_valid(self) -> bool:
        raise RuntimeError("evidence validation failed")


class FatalClockError(BaseException):
    pass


NOW = datetime(2026, 7, 22, tzinfo=UTC)


def _evidence(**updates: object) -> RunCapabilityEvidence:
    values: dict[str, object] = {
        "correlation_id": "corr-1",
        "run_id": "corr-1",
        "governance_level": GovernanceLevel.ENFORCED,
        "observed_at": NOW,
        "graph_version": "graph-v1",
        "signature_valid": True,
        "tool_manifest_complete": True,
    }
    values.update(updates)
    return RunCapabilityEvidence(**values)


@pytest.mark.asyncio
async def test_explicit_run_lookup_differs_from_legacy_correlation_lookup() -> None:
    class EvidenceByIdentity:
        def __init__(self, evidence: tuple[RunCapabilityEvidence, ...]) -> None:
            self.evidence = evidence

        async def evidence_for_governance_run(
            self, governance_run_id: str
        ) -> RunCapabilityEvidence | None:
            return next(
                (item for item in self.evidence if item.run_id == governance_run_id), None
            )

        async def evidence_for_run(
            self, correlation_id: str
        ) -> RunCapabilityEvidence | None:
            matches = [item for item in self.evidence if item.correlation_id == correlation_id]
            return matches[0] if len(matches) == 1 else None

    evidence = (
        _evidence(correlation_id="shared", run_id="run-1"),
        _evidence(correlation_id="shared", run_id="run-2"),
        _evidence(correlation_id="unique", run_id="run-3"),
    )
    reporter = CapabilityReporter(EvidenceByIdentity(evidence), now=lambda: NOW)

    assert (
        await reporter.level_for_governance_run("run-1", graph_version="graph-v1")
        is GovernanceLevel.ENFORCED
    )
    with pytest.warns(DeprecationWarning):
        unique = await reporter.level_for_run("unique", graph_version="graph-v1")
    with pytest.warns(DeprecationWarning):
        ambiguous = await reporter.level_for_run("shared", graph_version="graph-v1")

    assert unique is GovernanceLevel.ENFORCED
    assert ambiguous is GovernanceLevel.ADMISSION


@pytest.mark.asyncio
async def test_distinct_legacy_and_signed_run_providers_are_routed_by_identity() -> None:
    legacy = StaticEvidenceProvider(_evidence(correlation_id="legacy", run_id="legacy"))
    signed = StaticEvidenceProvider(_evidence(correlation_id="signed", run_id="signed"))
    reporter = CapabilityReporter(
        legacy,
        governance_evidence_provider=signed,
        now=lambda: NOW,
    )

    assert await reporter.level_for_run("legacy") is GovernanceLevel.ENFORCED
    assert await reporter.level_for_governance_run("signed") is GovernanceLevel.ENFORCED


@pytest.mark.asyncio
async def test_foundation_provider_reports_admission_without_evidence() -> None:
    reporter = CapabilityReporter(NoCapabilityEvidenceProvider(), now=lambda: NOW)

    assert (
        await reporter.level_for_run("corr-1", graph_version="graph-v1")
        is GovernanceLevel.ADMISSION
    )


@pytest.mark.asyncio
async def test_heartbeat_without_run_attestation_does_not_upgrade_run() -> None:
    heartbeat = _evidence(correlation_id="deployment-heartbeat")
    reporter = CapabilityReporter(NoCapabilityEvidenceProvider(), now=lambda: NOW)

    assert (
        await reporter.level_for_run(
            "corr-1", graph_version="graph-v1", deployment_evidence=heartbeat
        )
        is GovernanceLevel.ADMISSION
    )


@pytest.mark.asyncio
async def test_partial_tool_manifest_is_clamped_to_observed() -> None:
    reporter = CapabilityReporter(
        StaticEvidenceProvider(_evidence(tool_manifest_complete=False)), now=lambda: NOW
    )

    assert (
        await reporter.level_for_run("corr-1", graph_version="graph-v1") is GovernanceLevel.OBSERVED
    )


@pytest.mark.asyncio
async def test_complete_fresh_valid_evidence_reports_enforced() -> None:
    reporter = CapabilityReporter(StaticEvidenceProvider(_evidence()), now=lambda: NOW)

    assert (
        await reporter.level_for_run("corr-1", graph_version="graph-v1") is GovernanceLevel.ENFORCED
    )


@pytest.mark.asyncio
async def test_run_lookup_validates_signed_governance_run_identity() -> None:
    reporter = CapabilityReporter(
        StaticEvidenceProvider(_evidence(run_id="run-1")), now=lambda: NOW
    )

    assert (
        await reporter.level_for_governance_run("run-1", graph_version="graph-v1")
        is GovernanceLevel.ENFORCED
    )
    assert (
        await reporter.level_for_governance_run("run-2", graph_version="graph-v1")
        is GovernanceLevel.ADMISSION
    )


def test_stale_deployment_evidence_falls_back_to_admission() -> None:
    reporter = CapabilityReporter(
        NoCapabilityEvidenceProvider(), stale_after_seconds=90, now=lambda: NOW
    )

    level = reporter.level_for_deployment(_evidence(observed_at=NOW - timedelta(seconds=91)))

    assert level is GovernanceLevel.ADMISSION


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "evidence",
    [
        _evidence(signature_valid=False),
        _evidence(graph_version="graph-v2"),
    ],
)
async def test_invalid_or_mismatched_run_evidence_falls_back_to_admission(
    evidence: RunCapabilityEvidence,
) -> None:
    reporter = CapabilityReporter(StaticEvidenceProvider(evidence), now=lambda: NOW)

    assert (
        await reporter.level_for_run("corr-1", graph_version="graph-v1")
        is GovernanceLevel.ADMISSION
    )


@pytest.mark.asyncio
async def test_correlation_mismatch_falls_back_to_admission() -> None:
    reporter = CapabilityReporter(
        StaticEvidenceProvider(_evidence(correlation_id="other-correlation")), now=lambda: NOW
    )

    assert (
        await reporter.level_for_run("corr-1", graph_version="graph-v1")
        is GovernanceLevel.ADMISSION
    )


@pytest.mark.asyncio
async def test_future_evidence_falls_back_to_admission() -> None:
    reporter = CapabilityReporter(
        StaticEvidenceProvider(_evidence(observed_at=NOW + timedelta(seconds=1))),
        now=lambda: NOW,
    )

    assert (
        await reporter.level_for_run("corr-1", graph_version="graph-v1")
        is GovernanceLevel.ADMISSION
    )


@pytest.mark.asyncio
async def test_falsy_provider_is_preserved() -> None:
    reporter = CapabilityReporter(FalsyEvidenceProvider(_evidence()), now=lambda: NOW)

    assert (
        await reporter.level_for_run("corr-1", graph_version="graph-v1") is GovernanceLevel.ENFORCED
    )


@pytest.mark.asyncio
async def test_empty_graph_version_is_an_exact_override() -> None:
    reporter = CapabilityReporter(
        StaticEvidenceProvider(_evidence()),
        expected_graph_version="graph-v1",
        now=lambda: NOW,
    )

    assert await reporter.level_for_run("corr-1", graph_version="") is GovernanceLevel.ADMISSION


@pytest.mark.asyncio
async def test_provider_failure_falls_back_to_admission() -> None:
    reporter = CapabilityReporter(RaisingEvidenceProvider(), now=lambda: NOW)

    assert (
        await reporter.level_for_run("corr-1", graph_version="graph-v1")
        is GovernanceLevel.ADMISSION
    )


@pytest.mark.parametrize("error", [RuntimeError("clock failed"), OSError("clock failed")])
def test_clock_failure_falls_back_to_admission(error: Exception) -> None:
    def broken_clock() -> datetime:
        raise error

    reporter = CapabilityReporter(now=broken_clock)

    assert reporter.level_for_deployment(_evidence()) is GovernanceLevel.ADMISSION


def test_evidence_validation_failure_falls_back_to_admission() -> None:
    reporter = CapabilityReporter(now=lambda: NOW)

    assert (
        reporter.level_for_deployment(RaisingEvidence())  # type: ignore[arg-type]
        is GovernanceLevel.ADMISSION
    )


def test_base_exception_from_clock_propagates() -> None:
    def interrupted_clock() -> datetime:
        raise FatalClockError

    reporter = CapabilityReporter(now=interrupted_clock)

    with pytest.raises(FatalClockError):
        reporter.level_for_deployment(_evidence())


@pytest.mark.parametrize("stale_after", [0.0, -1.0, math.nan, math.inf, -math.inf])
def test_reporter_rejects_nonpositive_or_nonfinite_staleness(stale_after: float) -> None:
    with pytest.raises(ValueError, match="stale_after_seconds"):
        CapabilityReporter(stale_after_seconds=stale_after)
