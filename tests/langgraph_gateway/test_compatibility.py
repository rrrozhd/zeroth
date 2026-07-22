from __future__ import annotations

import json
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
    assert "0.11.2" in (result.reason or "")


@pytest.mark.asyncio
async def test_malformed_info_is_unsupported() -> None:
    transport, _ = _transport(info=["not", "an", "object"])

    result = await _detect(transport)

    assert result.status is CompatibilityStatus.UNSUPPORTED
    assert result.reason == "upstream /info response is malformed"


@pytest.mark.asyncio
async def test_changed_openapi_shape_is_unsupported() -> None:
    document = _openapi_document()
    document["paths"]["/threads"]["post"]["operationId"] = "changed_operation"  # type: ignore[index]
    transport, _ = _transport(openapi=document)

    result = await _detect(transport)

    assert result.status is CompatibilityStatus.UNSUPPORTED
    assert result.reason == "upstream OpenAPI fingerprint is not in the tested matrix"


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


NOW = datetime(2026, 7, 22, tzinfo=UTC)


def _evidence(**updates: object) -> RunCapabilityEvidence:
    values: dict[str, object] = {
        "correlation_id": "corr-1",
        "governance_level": GovernanceLevel.ENFORCED,
        "observed_at": NOW,
        "graph_version": "graph-v1",
        "signature_valid": True,
        "tool_manifest_complete": True,
    }
    values.update(updates)
    return RunCapabilityEvidence(**values)


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
