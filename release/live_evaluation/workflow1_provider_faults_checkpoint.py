"""Seal the purpose-specific Workflow 1 deterministic provider-fault matrix."""

from __future__ import annotations

import json
import struct
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .evidence import AcceptanceCriterion, CorrelationIds, EvidenceStore
from .workflow1_provider_faults_live import (
    EXPECTED_MODES,
    validate_provider_fault_summary,
)

ACCEPTED_CRITERIA = (
    "workflow1.negative-provider-timeout",
    "workflow1.negative-rate-limit",
    "workflow1.negative-malformed-response",
    "workflow1.deterministic-provider-fault-injection",
)


def _image_dimensions(path: Path) -> tuple[int, int]:
    payload = path.read_bytes()
    if payload.startswith(b"\x89PNG\r\n\x1a\n") and len(payload) >= 24:
        return struct.unpack(">II", payload[16:24])
    if payload.startswith(b"\xff\xd8"):
        offset = 2
        while offset + 9 <= len(payload):
            if payload[offset] != 0xFF:
                offset += 1
                continue
            marker = payload[offset + 1]
            offset += 2
            if marker in {0xD8, 0xD9}:
                continue
            if offset + 2 > len(payload):
                break
            length = int.from_bytes(payload[offset : offset + 2], "big")
            if length < 2 or offset + length > len(payload):
                break
            if marker in {
                0xC0,
                0xC1,
                0xC2,
                0xC3,
                0xC5,
                0xC6,
                0xC7,
                0xC9,
                0xCA,
                0xCB,
                0xCD,
                0xCE,
                0xCF,
            }:
                height = int.from_bytes(payload[offset + 3 : offset + 5], "big")
                width = int.from_bytes(payload[offset + 5 : offset + 7], "big")
                return width, height
            offset += length
    raise RuntimeError(f"browser screenshot dimensions are unreadable: {path.name}")


def _json(path: Path, *, label: str) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must be a JSON object")
    return value


def _browser_artifacts(source_root: Path) -> tuple[dict[str, Any], tuple[tuple[Path, Path], ...]]:
    index = _json(source_root / "evidence-index.json", label="browser evidence index")
    artifacts_value = index.get("artifacts")
    if not isinstance(artifacts_value, Sequence) or isinstance(
        artifacts_value, (str, bytes, bytearray)
    ):
        raise RuntimeError("browser evidence index lacks artifacts")
    artifacts: list[tuple[Path, Path]] = []
    screenshot_count = 0
    required_prefixes = {"screenshots", "network", "handoff"}
    observed_prefixes: set[str] = set()
    for raw in artifacts_value:
        if not isinstance(raw, Mapping):
            raise RuntimeError("browser evidence index contains an invalid artifact")
        source_text = raw.get("source")
        destination_text = raw.get("destination")
        if not isinstance(source_text, str) or not isinstance(destination_text, str):
            raise RuntimeError("browser evidence artifact paths are invalid")
        source = (source_root / source_text).resolve(strict=True)
        if source_root.resolve() not in source.parents:
            raise RuntimeError("browser evidence source escaped its root")
        destination = Path(destination_text)
        if destination.is_absolute() or ".." in destination.parts or not destination.parts:
            raise RuntimeError("browser evidence destination escaped its root")
        observed_prefixes.add(destination.parts[0])
        if destination.parts[0] == "screenshots":
            screenshot_count += 1
            if _image_dimensions(source) != (1440, 900):
                raise RuntimeError(
                    f"browser screenshot is not the required 1440x900 viewport: {source.name}"
                )
        artifacts.append((source, destination))
    if screenshot_count < 9 or not required_prefixes.issubset(observed_prefixes):
        raise RuntimeError(
            "browser evidence lacks nine screenshots, network, or UI observation proof"
        )
    return index, tuple(artifacts)


def build_checkpoint(*, source_root: Path, destination: Path) -> Path:
    """Validate unsealed UI/runtime proof and create one immutable evidence bundle."""
    source_root = source_root.expanduser().resolve(strict=True)
    destination = destination.expanduser().resolve(strict=False)
    if destination.exists():
        raise FileExistsError(destination)
    summary = _json(source_root / "runtime/summary.json", label="runtime summary")
    deployment = summary.get("deployment_ref")
    graph = summary.get("graph_version_ref")
    if not isinstance(deployment, str) or not isinstance(graph, str):
        raise RuntimeError("provider-fault summary lacks the served identity")
    validated = validate_provider_fault_summary(
        summary,
        expected_deployment_ref=deployment,
        expected_graph_version_ref=graph,
    )
    browser_index, browser_artifacts = _browser_artifacts(source_root / "browser")

    store = EvidenceStore(destination)
    store.write_manifest(
        {
            "schema_version": 1,
            "checkpoint": "workflow1-deterministic-provider-negative-matrix",
            "created_at": datetime.now(UTC).isoformat(),
            "tenant_id": "evaluation-studio-v1",
            "deployment_ref": deployment,
            "graph_version_ref": graph,
            "run_ids": validated["run_ids"],
            "fault_modes": list(EXPECTED_MODES),
            "provider_calls_performed": 0,
            "accepted_criteria": list(ACCEPTED_CRITERIA),
            "d012_restored": True,
        }
    )
    paths = ["manifest.json", "runtime/summary.json", "playwright-report/evidence-index.json"]
    store._write_exclusive(Path("runtime/summary.json"), summary)
    store._write_exclusive(Path("playwright-report/evidence-index.json"), browser_index)
    for source, relative in browser_artifacts:
        store.ingest_artifact(source, relative)
        paths.append(relative.as_posix())
    event_id = store.append_event(
        "campaign.workflow1.deterministic_provider_faults.verified",
        {
            "result": "pass",
            "fault_modes": list(EXPECTED_MODES),
            "faults_consumed": 3,
            "failed_runs": 3,
            "signed_audit_chains": 3,
            "provider_call_count": 0,
            "cost_event_count": 0,
            "total_cost_usd": 0.0,
            "d012_restored": True,
        },
        correlation=CorrelationIds(run_id=str(validated["run_ids"][0])),
    )
    evidence = tuple([*paths, f"events.ndjson#{event_id}"])
    store.finalize_bundle(
        acceptance=tuple(
            AcceptanceCriterion(criterion, "pass", evidence) for criterion in ACCEPTED_CRITERIA
        ),
        report_markdown=(
            "# Workflow 1 deterministic provider-negative matrix\n\n"
            "The real Studio UI submitted three independent runs against one published "
            "agent graph. Campaign-scoped one-shot faults produced timeout, rate-limit, "
            "and malformed-response failures before any external provider call. Each "
            "fault row was consumed exactly once, each failed run survived refresh, and "
            "all three signed run audit chains verify. Provider request IDs, cost event "
            "IDs, priced calls, and spend are all absent or zero. The pre-existing D-012 "
            "serving identity was restored exactly. This checkpoint does not claim live-"
            "provider execution or acceptance of the separate bad-credential criterion.\n"
        ),
    )
    return destination
