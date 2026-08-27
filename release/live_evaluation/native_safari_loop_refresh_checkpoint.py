"""Seal a provider-free loop run and native Safari refresh-restoration proof."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .evidence import AcceptanceCriterion, CorrelationIds, EvidenceStore
from .workflow3_lifecycle_evidence import STATE_ROOT

TENANT = "evaluation-studio-v1"
DEPLOYMENT = "demo-data-quality-repair-loop"
DEPLOYMENT_VERSION = 1
GRAPH = "06a4c062-5134-4066-a286-cf9da0109b39@1"
NODES = ("start", "inspect", "finalize")
RUN_ID = re.compile(r"^[a-f0-9]{32}$")

SOURCE_ROOT = (
    STATE_ROOT / "evidence/native-safari-loop-refresh-staging-20260825-1"
)
ROOT = STATE_ROOT / "evidence/native-safari-loop-refresh-checkpoint-20260825-1"

ACCEPTED_CRITERIA = (
    "ui.loop-configuration",
    "audit.every-node-timeline-entry",
    "audit.signed-chain-verifies",
)

RUNTIME_FILES = (
    "health.json",
    "run.json",
    "timeline.json",
    "audits.json",
    "run-audit-verification.json",
    "run-evidence.json",
    "deployment-cost.json",
)
SCREENSHOT_FILES = (
    "01-loop-succeeded-before-refresh-native-safari.jpg",
    "02-loop-succeeded-after-refresh-native-safari.jpg",
)
ACCESSIBILITY_FILES = (
    "01-loop-succeeded-before-refresh-native-safari.txt",
    "02-loop-succeeded-after-refresh-native-safari.txt",
)


def _object(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must be a JSON object")
    return value


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"missing {label}")
    try:
        return _object(json.loads(path.read_text()), label=label)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{label} is not valid JSON") from exc


def _sequence(value: Any, *, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise RuntimeError(f"{label} must be an array of objects")
    return value


def _validate_health(health: Mapping[str, Any]) -> None:
    if {
        "status": health.get("status"),
        "campaign_id": health.get("campaign_id"),
        "deployment_ref": health.get("deployment_ref"),
        "deployment_version": health.get("deployment_version"),
        "graph_version_ref": health.get("graph_version_ref"),
    } != {
        "status": "ok",
        "campaign_id": TENANT,
        "deployment_ref": DEPLOYMENT,
        "deployment_version": DEPLOYMENT_VERSION,
        "graph_version_ref": GRAPH,
    }:
        raise RuntimeError("health does not prove the exact loop deployment")


def _validate_run(run: Mapping[str, Any]) -> str:
    run_id = run.get("run_id")
    terminal = run.get("terminal_output")
    result = terminal.get("result") if isinstance(terminal, Mapping) else None
    if (
        not isinstance(run_id, str)
        or not RUN_ID.fullmatch(run_id)
        or run.get("thread_id") != run_id
        or run.get("status") != "succeeded"
        or run.get("deployment_ref") != DEPLOYMENT
        or run.get("graph_version_ref") != GRAPH
        or run.get("campaign_id") != TENANT
        or run.get("failure_state") is not None
        or not isinstance(terminal, Mapping)
        or terminal.get("quality_score") != 1.0
        or terminal.get("repair_pass") != 0
        or terminal.get("needs_repair") is not False
        or terminal.get("quality_issues") != []
        or not isinstance(result, Mapping)
        or result.get("status") != "ready"
        or result.get("quality_score") != 1.0
        or result.get("remaining_issues") != 0
        or result.get("repair_passes") != 0
        or run.get("audit_refs") != ["audit:1", "audit:2", "audit:3"]
    ):
        raise RuntimeError("run does not prove the exact provider-free loop success")
    return run_id


def _validate_timeline(timeline: Mapping[str, Any], *, run_id: str) -> None:
    entries = _sequence(timeline.get("entries"), label="timeline entries")
    if [entry.get("node_id") for entry in entries] != list(NODES):
        raise RuntimeError("timeline does not contain every expected loop node exactly once")
    for entry in entries:
        if (
            entry.get("run_id") != run_id
            or entry.get("thread_id") != run_id
            or entry.get("status") != "completed"
            or entry.get("deployment_ref") != DEPLOYMENT
            or entry.get("graph_version_ref") != GRAPH
            or entry.get("cost_usd") != 0.0
            or entry.get("estimated_cost_usd") != 0.0
            or entry.get("cost_event_id") is not None
        ):
            raise RuntimeError("timeline entry is not exactly correlated and zero-cost")


def _validate_audits(audits: Mapping[str, Any], *, run_id: str) -> None:
    records = _sequence(audits.get("records"), label="audit records")
    if [record.get("node_id") for record in records] != list(NODES):
        raise RuntimeError("audit records do not cover every expected loop node")
    for sequence, record in enumerate(records, start=1):
        digest = record.get("record_digest")
        signature = record.get("record_signature")
        if (
            record.get("run_id") != run_id
            or record.get("status") != "completed"
            or record.get("deployment_ref") != DEPLOYMENT
            or record.get("graph_version_ref") != GRAPH
            or record.get("chain_sequence") != sequence
            or not isinstance(digest, str)
            or len(digest) != 64
            or not isinstance(signature, str)
            or len(signature) != 64
            or record.get("signing_key_id") != "dev-local"
            or record.get("signing_algorithm") != "HS256"
            or record.get("cost_usd") != 0.0
            or record.get("estimated_cost_usd") != 0.0
            or record.get("cost_event_id") is not None
        ):
            raise RuntimeError("audit record is not signed, correlated, and zero-cost")


def _validate_verification(verification: Mapping[str, Any], *, run_id: str) -> None:
    if {
        "scope": verification.get("scope"),
        "verified": verification.get("verified"),
        "signature_verified": verification.get("signature_verified"),
        "record_count": verification.get("record_count"),
        "unsigned_record_count": verification.get("unsigned_record_count"),
        "signing_key_id": verification.get("signing_key_id"),
        "failed_audit_id": verification.get("failed_audit_id"),
        "error": verification.get("error"),
    } != {
        "scope": f"run:{run_id}",
        "verified": True,
        "signature_verified": True,
        "record_count": 3,
        "unsigned_record_count": 0,
        "signing_key_id": "dev-local",
        "failed_audit_id": None,
        "error": None,
    }:
        raise RuntimeError("run audit chain is not exactly signed and verified")


def _validate_economics(
    evidence: Mapping[str, Any], cost: Mapping[str, Any], *, run_id: str
) -> None:
    summary = evidence.get("summary")
    evidence_run = evidence.get("run")
    if (
        not isinstance(summary, Mapping)
        or not isinstance(evidence_run, Mapping)
        or evidence_run.get("run_id") != run_id
        or summary.get("audit_count") != 3
        or summary.get("priced_call_count") != 0
        or summary.get("cost_event_count") != 0
        or summary.get("total_cost_usd") != 0.0
        or summary.get("cost_identity_state") != "not_applicable_no_priced_call"
        or summary.get("reconciliation_state") != "reconciled_zero_activity"
        or cost.get("deployment_ref") != DEPLOYMENT
        or cost.get("currency") != "USD"
        or any(
            cost.get(field) != 0.0
            for field in (
                "total_cost_usd",
                "paid_spend_usd",
                "estimated_spend_usd",
                "unmeasured_spend_usd",
                "active_exposure_usd",
                "ambiguous_exposure_usd",
            )
        )
    ):
        raise RuntimeError("loop run does not have reconciled zero-activity economics")


def _validate_browser(source_root: Path, *, run_id: str) -> tuple[Path, ...]:
    artifacts: list[Path] = []
    for name in SCREENSHOT_FILES:
        path = source_root / "screenshots" / name
        if (
            path.is_symlink()
            or not path.is_file()
            or not path.read_bytes().startswith(b"\xff\xd8\xff")
        ):
            raise RuntimeError(f"invalid native Safari screenshot: {name}")
        artifacts.append(path)
    for name in ACCESSIBILITY_FILES:
        path = source_root / "accessibility" / name
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"missing native Safari accessibility snapshot: {name}")
        text = path.read_text()
        if "Run Succeeded" not in text or run_id not in text:
            raise RuntimeError("native Safari refresh did not restore the same run identity")
        artifacts.append(path)
    return tuple(artifacts)


def _load_runtime(source_root: Path) -> dict[str, dict[str, Any]]:
    return {
        name.removesuffix(".json"): _load_json(
            source_root / "runtime" / name, label=name
        )
        for name in RUNTIME_FILES
    }


def build_checkpoint(*, source_root: Path, destination: Path) -> Path:
    """Validate native UI plus runtime/audit/economics joins and seal them."""
    source_root = source_root.expanduser().resolve(strict=True)
    destination = destination.expanduser().resolve(strict=False)
    if destination.exists():
        raise FileExistsError(destination)
    records = _load_runtime(source_root)
    _validate_health(records["health"])
    run_id = _validate_run(records["run"])
    _validate_timeline(records["timeline"], run_id=run_id)
    _validate_audits(records["audits"], run_id=run_id)
    _validate_verification(records["run-audit-verification"], run_id=run_id)
    _validate_economics(
        records["run-evidence"], records["deployment-cost"], run_id=run_id
    )
    browser_artifacts = _validate_browser(source_root, run_id=run_id)

    store = EvidenceStore(destination)
    store.write_manifest(
        {
            "schema_version": 1,
            "checkpoint": "native-safari-provider-free-loop-refresh",
            "created_at": datetime.now(UTC).isoformat(),
            "tenant_id": TENANT,
            "deployment_ref": DEPLOYMENT,
            "deployment_version": DEPLOYMENT_VERSION,
            "graph_version_ref": GRAPH,
            "run_id": run_id,
            "provider_calls_performed": 0,
            "native_safari_screenshot_count": len(SCREENSHOT_FILES),
            "native_safari_accessibility_snapshot_count": len(ACCESSIBILITY_FILES),
            "accepted_criteria": list(ACCEPTED_CRITERIA),
        }
    )
    evidence_paths: list[str] = []
    for name, value in records.items():
        relative = Path("runtime") / f"{name}.json"
        store._write_exclusive(relative, value)
        evidence_paths.append(relative.as_posix())
    for source in browser_artifacts:
        top_level = "screenshots" if source.suffix.lower() in {".jpg", ".jpeg"} else "accessibility"
        relative = Path(top_level) / source.name
        store.ingest_artifact(source, relative)
        evidence_paths.append(relative.as_posix())

    event_id = store.append_event(
        "campaign.native_safari.loop_refresh_verified",
        {
            "result": "pass",
            "deployment_ref": DEPLOYMENT,
            "graph_version_ref": GRAPH,
            "node_ids": list(NODES),
            "provider_call_count": 0,
            "cost_event_count": 0,
            "total_cost_usd": 0.0,
            "proof_paths": evidence_paths,
        },
        correlation=CorrelationIds(run_id=run_id),
    )
    acceptance_evidence = tuple([*evidence_paths, f"events.ndjson#{event_id}"])
    store.finalize_bundle(
        acceptance=tuple(
            AcceptanceCriterion(criterion, "pass", acceptance_evidence)
            for criterion in ACCEPTED_CRITERIA
        ),
        report_markdown=(
            "# Native Safari provider-free loop refresh checkpoint\n\n"
            f"Native Safari submitted `{run_id}` through the published Customer data "
            f"quality repair graph `{GRAPH}` served by `{DEPLOYMENT}`. The run succeeded "
            "with the start, inspect, and finalize nodes completed. Safari displayed the "
            "same run identity and Succeeded state before and after a real browser reload. "
            "The three-record HMAC chain verifies with `dev-local`. No provider call or "
            "cost event occurred, and runtime evidence reconciles total spend to `$0.00`. "
            "This is local keyed-integrity evidence, not non-repudiation.\n"
        ),
    )
    return destination


def main() -> int:
    root = build_checkpoint(source_root=SOURCE_ROOT, destination=ROOT)
    print(json.dumps({"root": str(root), "sealed": EvidenceStore(root).is_sealed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
