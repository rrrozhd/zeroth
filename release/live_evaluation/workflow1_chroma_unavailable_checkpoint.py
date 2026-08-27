"""Seal provider-free Workflow 1 Chroma-unavailable runtime evidence."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .evidence import AcceptanceCriterion, CorrelationIds, EvidenceStore
from .workflow3_lifecycle_evidence import STATE_ROOT, _request

RUN_ID = "04e6af617a55429b987ad9b2aacdf848"
DEPLOYMENT = "evaluation-studio-v1-grounded-researcher-v1"
GRAPH = "evaluation-studio-v1-grounded-researcher@4"
TENANT = "evaluation-studio-v1"
ROOT = STATE_ROOT / "evidence/workflow1-chroma-unavailable-checkpoint-20260824-2"
SCREENSHOT_ROOT = STATE_ROOT / "evidence/workflow1-negative-chroma-safari-20260824-1/screenshots"
SCREENSHOT_SOURCES = (
    SCREENSHOT_ROOT / "02-exact-configured-run-native-safari.jpg",
    SCREENSHOT_ROOT / "03-failed-at-retrieve-native-safari.jpg",
    SCREENSHOT_ROOT / "04-runs-detail-native-safari.jpg",
    SCREENSHOT_ROOT / "05-run-chain-verified-native-safari.jpg",
    SCREENSHOT_ROOT / "08-deployment-audit-chain-intact-native-safari.jpg",
    SCREENSHOT_ROOT / "09-run-detail-1440x900-playwright.png",
    SCREENSHOT_ROOT / "10-run-chain-verified-1440x900-playwright.png",
    SCREENSHOT_ROOT / "11-deployment-audit-chain-verified-1440x900-playwright.png",
)

ACCEPTED_CRITERIA = ("workflow1.negative-chroma-unavailable",)

Request = Callable[..., Any]

_SUMMARY_FIELDS = (
    "audit_count",
    "approval_count",
    "tool_call_count",
    "memory_interaction_count",
    "priced_call_count",
    "cost_event_count",
    "total_cost_usd",
    "cost_identity_state",
    "reconciliation_state",
)
_VERIFICATION_FIELDS = (
    "scope",
    "verified",
    "signature_verified",
    "record_count",
    "unsigned_record_count",
    "failed_audit_id",
    "error",
    "signing_key_id",
)
_COST_FIELDS = (
    "deployment_ref",
    "total_cost_usd",
    "paid_spend_usd",
    "estimated_spend_usd",
    "unmeasured_spend_usd",
    "active_exposure_usd",
    "ambiguous_exposure_usd",
    "currency",
)


def _object(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} response must be a JSON object")
    return value


def _sanitize_health(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value.get(key)
        for key in (
            "status",
            "campaign_id",
            "deployment_ref",
            "deployment_version",
            "graph_version_ref",
        )
    }


def _sanitize_run(value: Mapping[str, Any]) -> dict[str, Any]:
    failure = value.get("failure_state")
    failure = failure if isinstance(failure, Mapping) else {}
    return {
        "run_id": value.get("run_id"),
        "status": value.get("status"),
        "deployment_ref": value.get("deployment_ref"),
        "graph_version_ref": value.get("graph_version_ref"),
        "tenant_id": value.get("tenant_id"),
        "campaign_id": value.get("campaign_id"),
        "failure_reason": failure.get("reason"),
    }


def _sanitize_audit(value: Mapping[str, Any]) -> dict[str, Any]:
    metadata = value.get("execution_metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    return {
        "audit_id": value.get("audit_id"),
        "run_id": value.get("run_id"),
        "node_id": value.get("node_id"),
        "chain_sequence": value.get("chain_sequence"),
        "status": value.get("status"),
        "attempt": value.get("attempt"),
        "record_digest": value.get("record_digest"),
        "previous_record_digest": value.get("previous_record_digest"),
        "record_signature_present": bool(value.get("record_signature")),
        "signing_key_id": value.get("signing_key_id"),
        "signing_algorithm": value.get("signing_algorithm"),
        # Preserve the runtime distinction between measured zero-cost control
        # nodes and the failed retrieval, which exited before measurement.
        # Zero priced calls is proven by the evidence summary and missing cost
        # identity, not by manufacturing a numeric zero for an unmeasured node.
        "cost_usd": value.get("cost_usd"),
        "cost_event_id": value.get("cost_event_id"),
        "provider_request_id": metadata.get("provider_request_id"),
    }


def _sanitize_audits(value: Any, *, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise RuntimeError(f"{label} audits must be a JSON array")
    if not all(isinstance(row, Mapping) for row in value):
        raise RuntimeError(f"{label} contains an invalid audit record")
    return [_sanitize_audit(row) for row in value]


def _sanitize_timeline(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "deployment_ref": value.get("deployment_ref"),
        "run_id": value.get("run_id"),
        "entries": _sanitize_audits(value.get("entries"), label="timeline"),
    }


def _sanitize_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    run = value.get("run")
    summary = value.get("summary")
    approvals = value.get("approvals")
    if not isinstance(run, Mapping) or not isinstance(summary, Mapping):
        raise RuntimeError("run evidence is missing its run or summary object")
    if not isinstance(approvals, list):
        raise RuntimeError("run evidence approvals must be a JSON array")
    return {
        "run": _sanitize_run(run),
        "audits": _sanitize_audits(value.get("audits"), label="run evidence"),
        "approval_count": len(approvals),
        "summary": {key: summary.get(key) for key in _SUMMARY_FIELDS},
    }


def _sanitize_verification(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value.get(key) for key in _VERIFICATION_FIELDS}


def _sanitize_cost(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value.get(key) for key in _COST_FIELDS}


def _require_identity(value: Mapping[str, Any], *, label: str) -> None:
    if (
        value.get("deployment_ref") != DEPLOYMENT
        or value.get("graph_version_ref") != GRAPH
        or value.get("campaign_id") != TENANT
    ):
        raise RuntimeError(f"{label} identity does not match the exact Workflow 1 target")


def _validate_audits(audits: list[dict[str, Any]]) -> None:
    if len(audits) != 3:
        raise RuntimeError("Workflow 1 checkpoint requires exactly three audit records")
    if [row.get("node_id") for row in audits] != ["request", "revision-loop", "retrieve"]:
        raise RuntimeError("Workflow 1 audit nodes must end at retrieve with research absent")
    if [row.get("status") for row in audits] != ["completed", "completed", "failed"]:
        raise RuntimeError("Workflow 1 retrieve failure disposition is invalid")
    if [row.get("chain_sequence") for row in audits] != [1, 2, 3]:
        raise RuntimeError("Workflow 1 audit sequence must be contiguous from one through three")
    if len({row.get("audit_id") for row in audits}) != 3 or any(
        not isinstance(row.get("audit_id"), str) or not row.get("audit_id") for row in audits
    ):
        raise RuntimeError("Workflow 1 audit identities are missing or duplicated")
    if any(row.get("run_id") != RUN_ID for row in audits):
        raise RuntimeError("Workflow 1 timeline includes an audit from a different run")
    if any(row.get("record_signature_present") is not True for row in audits):
        raise RuntimeError("Workflow 1 checkpoint requires exactly three signed audits")
    if any(
        not isinstance(row.get("record_digest"), str) or not row.get("record_digest")
        for row in audits
    ):
        raise RuntimeError("Workflow 1 signed audits are missing record digests")
    if audits[0].get("previous_record_digest") is not None or any(
        audits[index].get("previous_record_digest") != audits[index - 1].get("record_digest")
        for index in range(1, len(audits))
    ):
        raise RuntimeError("Workflow 1 audit digest continuity is broken")
    if [row.get("cost_usd") for row in audits] != [0.0, 0.0, None] or any(
        row.get("cost_event_id") is not None or row.get("provider_request_id") is not None
        for row in audits
    ):
        raise RuntimeError("Workflow 1 audit records contain priced-call or cost identity")


def _validate_verification(
    value: Mapping[str, Any],
    *,
    scope: str,
    exact_record_count: int | None,
) -> None:
    record_count = value.get("record_count")
    if (
        value.get("scope") != scope
        or value.get("verified") is not True
        or value.get("signature_verified") is not True
        or value.get("unsigned_record_count") != 0
        or value.get("failed_audit_id") is not None
        or value.get("error") is not None
        or not isinstance(record_count, int)
        or isinstance(record_count, bool)
        or record_count < 3
        or (exact_record_count is not None and record_count != exact_record_count)
    ):
        raise RuntimeError(f"signed audit verification failed for {scope}")


def _validate_cost(value: Mapping[str, Any]) -> None:
    if value.get("deployment_ref") != DEPLOYMENT or value.get("currency") != "USD":
        raise RuntimeError("deployment cost state targets the wrong deployment or currency")
    for field in _COST_FIELDS[1:-1]:
        number = value.get(field)
        if (
            not isinstance(number, (int, float))
            or isinstance(number, bool)
            or not math.isfinite(float(number))
            or float(number) < 0
        ):
            raise RuntimeError(f"deployment cost state has an invalid {field}")


def _validate(
    *,
    health: dict[str, Any],
    run: dict[str, Any],
    timeline: dict[str, Any],
    evidence: dict[str, Any],
    run_verification: dict[str, Any],
    deployment_verification: dict[str, Any],
    cost: dict[str, Any],
) -> None:
    _require_identity(health, label="health")
    if (
        health.get("status") != "ok"
        or not isinstance(health.get("deployment_version"), int)
        or isinstance(health.get("deployment_version"), bool)
        or health["deployment_version"] < 1
    ):
        raise RuntimeError("health identity is unavailable or invalid")

    _require_identity(run, label="run")
    if (
        run.get("run_id") != RUN_ID
        or run.get("tenant_id") != TENANT
        or run.get("status") != "failed"
        or run.get("failure_reason") != "node_execution_failed"
    ):
        raise RuntimeError("exact Workflow 1 run does not show the required failure")

    if timeline.get("deployment_ref") != DEPLOYMENT or timeline.get("run_id") != RUN_ID:
        raise RuntimeError("timeline identity does not match the exact Workflow 1 run")
    audits = timeline["entries"]
    _validate_audits(audits)

    evidence_run = evidence.get("run")
    if not isinstance(evidence_run, Mapping):
        raise RuntimeError("run evidence identity is missing")
    _require_identity(evidence_run, label="run evidence")
    if evidence_run != run or evidence.get("audits") != audits:
        raise RuntimeError("run evidence does not match the run and timeline sources")
    summary = evidence.get("summary")
    expected_summary = {
        "audit_count": 3,
        "approval_count": 0,
        "tool_call_count": 0,
        "memory_interaction_count": 0,
        "priced_call_count": 0,
        "cost_event_count": 0,
        "total_cost_usd": 0.0,
        "cost_identity_state": "not_applicable_no_priced_call",
        "reconciliation_state": "reconciled_zero_activity",
    }
    if evidence.get("approval_count") != 0 or summary != expected_summary:
        if isinstance(summary, Mapping) and summary.get("priced_call_count") != 0:
            raise RuntimeError("run evidence does not prove zero priced calls")
        raise RuntimeError("run evidence zero-activity cost identity is invalid")

    _validate_verification(
        run_verification,
        scope=f"run:{RUN_ID}",
        exact_record_count=3,
    )
    _validate_verification(
        deployment_verification,
        scope=f"deployment:{DEPLOYMENT}",
        exact_record_count=None,
    )
    _validate_cost(cost)


def _fetch_and_sanitize(request: Request) -> dict[str, dict[str, Any]]:
    health = _sanitize_health(_object(request("/health"), label="health"))
    run = _sanitize_run(_object(request(f"/v1/runs/{RUN_ID}"), label="run"))
    timeline = _sanitize_timeline(_object(request(f"/v1/runs/{RUN_ID}/timeline"), label="timeline"))
    evidence = _sanitize_evidence(
        _object(request(f"/v1/runs/{RUN_ID}/evidence"), label="run evidence")
    )
    run_verification = _sanitize_verification(
        _object(
            request(f"/v1/runs/{RUN_ID}/verify-chain", method="POST"),
            label="run-chain verification",
        )
    )
    deployment_verification = _sanitize_verification(
        _object(
            request(f"/v1/deployments/{DEPLOYMENT}/audit-verification"),
            label="deployment audit verification",
        )
    )
    cost = _sanitize_cost(
        _object(request(f"/v1/deployments/{DEPLOYMENT}/cost"), label="deployment cost")
    )
    return {
        "health": health,
        "run": run,
        "timeline": timeline,
        "run-evidence": evidence,
        "run-chain-verification": run_verification,
        "deployment-audit-verification": deployment_verification,
        "deployment-cost": cost,
    }


def _validated_screenshots(sources: Sequence[Path]) -> tuple[Path, ...]:
    screenshots = tuple(Path(source) for source in sources)
    if len(screenshots) < 5:
        raise RuntimeError("checkpoint requires at least five browser checkpoint screenshots")
    if len({source.name for source in screenshots}) != len(screenshots):
        raise RuntimeError("checkpoint screenshot names must be unique")
    for source in screenshots:
        if source.is_symlink() or not source.is_file():
            raise RuntimeError(f"checkpoint screenshot is unavailable: {source.name}")
        if source.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
            raise RuntimeError(f"checkpoint screenshot has the wrong media type: {source.name}")
    return screenshots


def build_checkpoint(
    *,
    destination: Path,
    request: Request,
    screenshot_sources: Sequence[Path],
) -> Path:
    """Fetch read-only runtime state, validate it, and seal a new evidence bundle."""
    destination = destination.expanduser().resolve(strict=False)
    if destination.exists():
        raise FileExistsError(destination)

    records = _fetch_and_sanitize(request)
    _validate(
        health=records["health"],
        run=records["run"],
        timeline=records["timeline"],
        evidence=records["run-evidence"],
        run_verification=records["run-chain-verification"],
        deployment_verification=records["deployment-audit-verification"],
        cost=records["deployment-cost"],
    )
    screenshots = _validated_screenshots(screenshot_sources)

    store = EvidenceStore(destination)
    created_at = datetime.now(UTC).isoformat()
    store.write_manifest(
        {
            "schema_version": 1,
            "checkpoint": "workflow1-provider-free-chroma-unavailable",
            "created_at": created_at,
            "run_id": RUN_ID,
            "deployment_ref": DEPLOYMENT,
            "graph_version_ref": GRAPH,
            "tenant_id": TENANT,
            "provider_calls_performed": 0,
            "fault_consumption_claimed": False,
            "fault_consumption_proof": "not_exposed_by_read_only_control_api",
            "accepted_criteria": list(ACCEPTED_CRITERIA),
            "source_count": len(records),
            "native_safari_screenshot_count": len(screenshots),
        }
    )
    evidence_paths: list[str] = []
    for name, record in records.items():
        relative = Path("runtime") / f"{name}.json"
        store._write_exclusive(relative, record)
        evidence_paths.append(relative.as_posix())
    for source in screenshots:
        relative = Path("screenshots") / source.name
        store.ingest_artifact(source, relative)
        evidence_paths.append(relative.as_posix())

    retrieve_audit = records["timeline"]["entries"][-1]
    event_id = store.append_event(
        "campaign.run.audit.chroma_unavailable_verified",
        {
            "result": "pass",
            "failure_reason": "node_execution_failed",
            "failed_node": "retrieve",
            "research_audit_count": 0,
            "signed_audit_count": 3,
            "priced_call_count": 0,
            "cost_identity_state": "not_applicable_no_priced_call",
            "proof_paths": evidence_paths,
        },
        correlation=CorrelationIds(
            run_id=RUN_ID,
            audit_event_id=str(retrieve_audit["audit_id"]),
        ),
    )
    acceptance_evidence = tuple([*evidence_paths, f"events.ndjson#{event_id}"])
    store.finalize_bundle(
        acceptance=tuple(
            AcceptanceCriterion(criterion, "pass", acceptance_evidence)
            for criterion in ACCEPTED_CRITERIA
        ),
        report_markdown=(
            "# Workflow 1 Chroma-unavailable checkpoint\n\n"
            f"Exact run `{RUN_ID}` failed at `retrieve` before `research` on deployment "
            f"`{DEPLOYMENT}` and graph `{GRAPH}`. Its three audit records are signed, "
            "the run chain verifies, and the deployment audit chain verifies. Run evidence "
            "reports zero priced calls, zero cost events, USD 0 run cost, and cost identity "
            "`not_applicable_no_priced_call`.\n\n"
            "The cumulative deployment cost snapshot is contextual; it is not used as a "
            "before/after run-cost delta. The run evidence summary and audit identities are "
            "the zero-priced-call proof. The control surface exposes fault arming but no "
            "read-only historical fault-state endpoint, so this checkpoint does not claim "
            "the one-shot connector fault row was consumed. The exact retrieve failure and "
            "evidence identity remain the accepted negative proof. Only the criterion listed "
            "in `acceptance.json` is accepted. No provider call was made while building this "
            "checkpoint.\n"
        ),
    )
    return destination


def main() -> int:
    root = build_checkpoint(
        destination=ROOT,
        request=_request,
        screenshot_sources=SCREENSHOT_SOURCES,
    )
    print(json.dumps({"root": str(root), "sealed": EvidenceStore(root).is_sealed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
