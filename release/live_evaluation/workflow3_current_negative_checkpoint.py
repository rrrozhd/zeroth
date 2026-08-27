"""Seal current-runtime Workflow 3 refresh/rejection and SLA negative evidence."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from .evidence import AcceptanceCriterion, CorrelationIds, EvidenceStore
from .workflow3_current_happy_checkpoint import (
    DEPLOYMENT,
    ECON_DB,
    GRAPH,
    MAIN_DB,
    SINK_DB,
    TENANT,
    Request,
    _json_text,
    _load_object,
    _one,
    _readonly,
)
from .workflow3_lifecycle_evidence import STATE_ROOT, _request

UI_ROOT = STATE_ROOT / "evidence/workflow3-current-negative-ui-20260824-3"
ROOT = STATE_ROOT / "evidence/workflow3-current-negative-checkpoint-20260824-1"

ACCEPTED_CRITERIA = (
    "workflow3.negative-refresh-before-approval",
    "workflow3.negative-sla-expiry",
)


@dataclass(frozen=True)
class NegativeUISource:
    refresh_run_id: str
    refresh_approval_id: str
    sla_run_id: str
    sla_approval_id: str
    json_artifacts: tuple[Path, ...]
    screenshots: tuple[Path, ...]
    videos: tuple[Path, ...]
    report: Path
    results: Path


def _load_negative_ui_source(root: Path) -> NegativeUISource:
    root = root.resolve(strict=True)
    results_path = root / "results.json"
    results = _load_object(results_path)
    criteria = results.get("criteria")
    dispositions = {
        row.get("criterion_id"): row.get("status")
        for row in criteria
        if isinstance(row, dict)
    } if isinstance(criteria, list) else {}
    if results.get("completed") is not True or dispositions != {
        criterion: "pass" for criterion in ACCEPTED_CRITERIA
    }:
        raise RuntimeError("negative Playwright source did not pass the exact allowlist")

    indexed = root / "indexed"
    refresh_path = _one(indexed, "*refresh-reject-runtime.json")
    sla_path = _one(indexed, "*sla-expiry-runtime.json")
    refresh = _load_object(refresh_path)
    sla = _load_object(sla_path)
    for label, value in (("refresh", refresh), ("SLA", sla)):
        if value.get("deployment_ref") != DEPLOYMENT or value.get("graph_version_ref") != GRAPH:
            raise RuntimeError(f"{label} runtime attachment targets the wrong graph")
        if not isinstance(value.get("run_id"), str) or not isinstance(
            value.get("approval_id"), str
        ):
            raise RuntimeError(f"{label} runtime attachment is missing identities")
    if sla.get("actor") != "sla_enforcer":
        raise RuntimeError("SLA runtime attachment is missing the sla_enforcer actor")
    screenshots = tuple(sorted(indexed.glob("*.png")))
    videos = tuple(sorted(indexed.glob("*.webm")))
    if len(screenshots) != 8:
        raise RuntimeError("negative Playwright source must contain exactly 8 screenshots")
    if len(videos) != 2:
        raise RuntimeError("negative Playwright source must contain exactly 2 videos")
    json_artifacts = tuple(
        sorted(
            {
                refresh_path,
                sla_path,
                *indexed.glob("*sanitized-console.json"),
                *indexed.glob("*sanitized-network.json"),
                *indexed.glob("*response-identities.json"),
            }
        )
    )
    if len(json_artifacts) != 8:
        raise RuntimeError("negative Playwright source is missing safe JSON summaries")
    return NegativeUISource(
        refresh_run_id=refresh["run_id"],
        refresh_approval_id=refresh["approval_id"],
        sla_run_id=sla["run_id"],
        sla_approval_id=sla["approval_id"],
        json_artifacts=json_artifacts,
        screenshots=screenshots,
        videos=videos,
        report=_one(root / "html-report", "index.html"),
        results=results_path,
    )


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise RuntimeError("negative checkpoint timestamp is missing")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _negative_audit(row: Any) -> dict[str, Any]:
    record = _json_text(row["record_json"], label="negative audit record")
    metadata = record.get("execution_metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    actor = record.get("actor")
    actor = actor if isinstance(actor, dict) else {}
    return {
        "audit_id": row["audit_id"],
        "node_id": row["node_id"],
        "chain_sequence": row["chain_sequence"],
        "record_digest": record.get("record_digest"),
        "previous_record_digest": record.get("previous_record_digest"),
        "record_signature_present": bool(record.get("record_signature")),
        "signing_key_id": record.get("signing_key_id"),
        "signing_algorithm": record.get("signing_algorithm"),
        "actor_subject": actor.get("subject"),
        "status": record.get("status"),
        "manifest_ref_sha256": metadata.get("manifest_ref_sha256"),
        "cost_usd": float(row["cost_usd"] or 0.0),
        "cost_event_id": row["cost_event_id"],
    }


def _collect_negative_proof(
    *,
    scenario: str,
    run_id: str,
    source_approval_id: str,
    request: Request,
    main_db: Path,
    sink_db: Path,
    econ_db: Path,
) -> dict[str, Any]:
    runtime = request(f"/v1/runs/{run_id}")
    evidence = request(f"/v1/runs/{run_id}/evidence")
    verification = request(f"/v1/runs/{run_id}/verify-chain", method="POST")
    if not all(isinstance(value, dict) for value in (runtime, evidence, verification)):
        raise RuntimeError(f"negative runtime evidence is invalid for {run_id}")
    with _readonly(main_db) as database:
        run_rows = database.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchall()
        approval_rows = database.execute(
            "SELECT * FROM approvals WHERE run_id = ?", (run_id,)
        ).fetchall()
        operation_count = int(
            database.execute(
                "SELECT COUNT(*) FROM side_effect_operations WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
        )
        audit_rows = database.execute(
            "SELECT * FROM node_audits WHERE run_id = ? ORDER BY chain_sequence", (run_id,)
        ).fetchall()
    if len(run_rows) != 1 or len(approval_rows) != 1:
        raise RuntimeError(f"negative persistence cardinality failed for {run_id}")
    run = run_rows[0]
    approval_row = approval_rows[0]
    approval_record = _json_text(approval_row["record_json"], label="approval record")
    resolution = approval_record.get("resolution")
    resolution = resolution if isinstance(resolution, dict) else {}
    actor = resolution.get("actor")
    actor = actor if isinstance(actor, dict) else {}
    audits = [_negative_audit(row) for row in audit_rows]
    api_audits = evidence.get("audits")
    api_approvals = evidence.get("approvals")
    api_digest_map = {
        row.get("audit_id"): row.get("record_digest")
        for row in api_audits
        if isinstance(row, dict)
    } if isinstance(api_audits, list) else {}
    api_approval_ids = {
        row.get("approval_id") for row in api_approvals if isinstance(row, dict)
    } if isinstance(api_approvals, list) else set()
    with _readonly(sink_db) as sink:
        markers_since_run = int(
            sink.execute(
                "SELECT COUNT(*) FROM action_markers WHERE created_at >= ?",
                (run["started_at"],),
            ).fetchone()[0]
        )
    with _readonly(econ_db) as economics:
        execution_count = int(
            economics.execute(
                """
                SELECT COUNT(*) FROM execution_events
                WHERE tenant_id = ? AND (execution_id = ? OR join_key = ?)
                """,
                (TENANT, run_id, run_id),
            ).fetchone()[0]
        )
        reservation_count = int(
            economics.execute(
                "SELECT COUNT(*) FROM cost_reservations WHERE tenant_id = ? AND run_id = ?",
                (TENANT, run_id),
            ).fetchone()[0]
        )
    failure = _json_text(run["failure_state"], label="negative failure state")
    action_manifest_audits = sum(
        row.get("node_id") == "synthetic-action"
        or row.get("manifest_ref_sha256") is not None
        for row in audits
    )
    return {
        "scenario": scenario,
        "run_id": run_id,
        "runtime": {
            "status": runtime.get("status"),
            "deployment_ref": runtime.get("deployment_ref"),
            "graph_version_ref": runtime.get("graph_version_ref"),
            "tenant_id": runtime.get("tenant_id"),
            "campaign_id": runtime.get("campaign_id"),
            "persistent_status": run["status"],
            "persistent_identity_matches": (
                run["deployment_ref"] == runtime.get("deployment_ref")
                and run["graph_version_ref"] == runtime.get("graph_version_ref")
                and run["tenant_id"] == runtime.get("tenant_id")
            ),
            "failure_reason": failure.get("reason"),
            "started_at": run["started_at"],
            "updated_at": run["updated_at"],
        },
        "approval": {
            "count": 1,
            "approval_id": approval_row["approval_id"],
            "source_approval_id": source_approval_id,
            "status": approval_row["status"],
            "decision": resolution.get("decision"),
            "actor_subject": actor.get("subject"),
            "created_at": approval_row["created_at"],
            "sla_deadline": approval_row["sla_deadline"],
            "resolved_at": resolution.get("resolved_at"),
            "refresh_identity_assertion_passed": scenario == "refresh_reject",
            "refresh_pending_state_assertion_passed": scenario == "refresh_reject",
        },
        "verification": {
            key: verification.get(key)
            for key in ("verified", "signature_verified", "record_count", "unsigned_record_count")
        },
        "audits": audits,
        "side_effects": {
            "operation_count": operation_count,
            "action_manifest_audit_count": action_manifest_audits,
            "markers_created_since_run": markers_since_run,
        },
        "economics": {
            "execution_event_count": execution_count,
            "reservation_count": reservation_count,
        },
        "runtime_evidence_matches_database": (
            api_digest_map == {row["audit_id"]: row["record_digest"] for row in audits}
            and approval_row["approval_id"] in api_approval_ids
        ),
    }


def _validate_negative_proof(proof: Mapping[str, Any]) -> None:
    scenario = proof.get("scenario")
    run_id = proof.get("run_id")
    runtime = proof.get("runtime")
    approval = proof.get("approval")
    verification = proof.get("verification")
    audits = proof.get("audits")
    side_effects = proof.get("side_effects")
    economics = proof.get("economics")
    if scenario not in {"refresh_reject", "sla_expiry"}:
        raise RuntimeError("unknown Workflow 3 negative scenario")
    if not all(
        isinstance(value, dict)
        for value in (runtime, approval, verification, side_effects, economics)
    ) or not isinstance(audits, list):
        raise RuntimeError(f"incomplete negative proof for {run_id}")
    if (
        runtime.get("status") != "failed"
        or runtime.get("persistent_status") != "FAILED"
        or runtime.get("deployment_ref") != DEPLOYMENT
        or runtime.get("graph_version_ref") != GRAPH
        or runtime.get("tenant_id") != TENANT
        or runtime.get("campaign_id") != TENANT
        or runtime.get("persistent_identity_matches") is not True
        or runtime.get("failure_reason") != "approval_rejected"
    ):
        raise RuntimeError(f"negative runtime identity/status failed for {run_id}")
    if (
        approval.get("count") != 1
        or approval.get("status") != "resolved"
        or approval.get("decision") != "reject"
    ):
        raise RuntimeError(f"negative approval did not resolve reject for {run_id}")
    if scenario == "refresh_reject" and (
        approval.get("approval_id") != approval.get("source_approval_id")
        or approval.get("refresh_identity_assertion_passed") is not True
        or approval.get("refresh_pending_state_assertion_passed") is not True
    ):
        raise RuntimeError("refresh did not preserve the identical approval ID and pending state")
    if scenario == "sla_expiry":
        if approval.get("actor_subject") != "sla_enforcer":
            raise RuntimeError("SLA rejection was not resolved by sla_enforcer")
        if _parse_time(approval.get("resolved_at")) <= _parse_time(
            approval.get("sla_deadline")
        ):
            raise RuntimeError("SLA rejection did not resolve after its deadline")
    if (
        verification.get("verified") is not True
        or verification.get("signature_verified") is not True
        or verification.get("unsigned_record_count") != 0
        or verification.get("record_count") != len(audits)
        or not audits
    ):
        raise RuntimeError(f"signed negative chain verification failed for {run_id}")
    sequences = [row.get("chain_sequence") for row in audits if isinstance(row, dict)]
    if sequences != list(range(1, len(audits) + 1)):
        raise RuntimeError(f"negative audit chain sequence failed for {run_id}")
    for index, row in enumerate(audits):
        if not isinstance(row, dict) or row.get("record_signature_present") is not True:
            raise RuntimeError(f"negative audit record is unsigned for {run_id}")
        if index and row.get("previous_record_digest") != audits[index - 1].get(
            "record_digest"
        ):
            raise RuntimeError(f"negative audit digest chain is broken for {run_id}")
    if any(
        isinstance(row, dict) and row.get("manifest_ref_sha256") is not None
        for row in audits
    ):
        raise RuntimeError(f"unexpected action-manifest audit for {run_id}")
    if side_effects != {
        "operation_count": 0,
        "action_manifest_audit_count": 0,
        "markers_created_since_run": 0,
    }:
        raise RuntimeError(f"zero-side-effect invariant failed for {run_id}")
    if economics != {"execution_event_count": 0, "reservation_count": 0}:
        raise RuntimeError(f"zero-economics invariant failed for {run_id}")
    if proof.get("runtime_evidence_matches_database") is not True:
        raise RuntimeError(f"runtime/database negative evidence differs for {run_id}")


def _ingest_ui(store: EvidenceStore, source: NegativeUISource) -> list[str]:
    paths: list[str] = []
    for original in source.json_artifacts:
        directory = "network" if "network" in original.name else "console"
        relative = Path(directory) / original.name
        store.ingest_artifact(original, relative)
        paths.append(relative.as_posix())
    for original in source.screenshots:
        relative = Path("screenshots") / original.name
        store.ingest_artifact(original, relative)
        paths.append(relative.as_posix())
    for original in source.videos:
        relative = Path("videos") / original.name
        store.ingest_artifact(original, relative)
        paths.append(relative.as_posix())
    for original, relative in (
        (source.report, Path("playwright-report/index.html")),
        (source.results, Path("playwright-report/results.json")),
    ):
        store.ingest_artifact(original, relative)
        paths.append(relative.as_posix())
    return paths


def build_checkpoint(
    *,
    destination: Path,
    ui_root: Path,
    main_db: Path,
    sink_db: Path,
    econ_db: Path,
    request: Request,
) -> Path:
    if destination.exists():
        raise RuntimeError(f"checkpoint already exists: {destination}")
    source = _load_negative_ui_source(ui_root)
    health = request("/health")
    if not isinstance(health, dict) or (
        health.get("status") != "ok"
        or health.get("deployment_ref") != DEPLOYMENT
        or health.get("graph_version_ref") != GRAPH
    ):
        raise RuntimeError("current health does not match the exact Workflow 3 graph")
    proofs = [
        _collect_negative_proof(
            scenario="refresh_reject",
            run_id=source.refresh_run_id,
            source_approval_id=source.refresh_approval_id,
            request=request,
            main_db=main_db,
            sink_db=sink_db,
            econ_db=econ_db,
        ),
        _collect_negative_proof(
            scenario="sla_expiry",
            run_id=source.sla_run_id,
            source_approval_id=source.sla_approval_id,
            request=request,
            main_db=main_db,
            sink_db=sink_db,
            econ_db=econ_db,
        ),
    ]
    for proof in proofs:
        _validate_negative_proof(proof)

    store = EvidenceStore(destination)
    store.write_manifest(
        {
            "schema_version": 1,
            "checkpoint": "workflow3-current-provider-free-negative-paths",
            "created_at": datetime.now(UTC).isoformat(),
            "source_root": ui_root.name,
            "source_results_sha256": sha256(source.results.read_bytes()).hexdigest(),
            "run_ids": [proof["run_id"] for proof in proofs],
            "deployment_ref": DEPLOYMENT,
            "graph_version_ref": GRAPH,
            "tenant_id": TENANT,
            "provider_calls_performed": 0,
            "accepted_criteria": list(ACCEPTED_CRITERIA),
        }
    )
    health_path = Path("runtime/health.json")
    store._write_exclusive(
        health_path,
        {
            key: health.get(key)
            for key in (
                "status",
                "campaign_id",
                "deployment_ref",
                "deployment_version",
                "graph_version_ref",
            )
        },
    )
    proof_paths: list[str] = []
    for index, proof in enumerate(proofs, start=1):
        relative = Path(f"runtime/{proof['scenario']}.json")
        store._write_exclusive(relative, proof)
        proof_paths.append(relative.as_posix())
        approval_audit = next(
            row for row in reversed(proof["audits"]) if row["node_id"] == "approval"
        )
        store.append_event(
            "campaign.run.audit.negative_verified",
            {
                "scenario": proof["scenario"],
                "result": "pass",
                "proof_path": relative.as_posix(),
                "sequence": index,
            },
            correlation=CorrelationIds(
                run_id=proof["run_id"], audit_event_id=approval_audit["audit_id"]
            ),
        )
    ui_paths = _ingest_ui(store, source)
    evidence = tuple([health_path.as_posix(), *proof_paths, *ui_paths])
    store.finalize_bundle(
        acceptance=tuple(
            AcceptanceCriterion(criterion, "pass", evidence)
            for criterion in ACCEPTED_CRITERIA
        ),
        report_markdown=(
            "# Workflow 3 current negative checkpoint\n\n"
            "The refresh/rejection and SLA-expiry UI journeys are rejoined to the exact "
            "current Workflow 3 deployment and graph. The refresh test preserves the "
            "same pending approval identity across reload before rejection. The SLA "
            "approval resolves reject by `sla_enforcer` after its deadline. Both runs "
            "have verified signed audit chains, zero operation rows, zero action-manifest "
            "audits, zero sink markers created since run start, and zero economics events "
            "or reservations. Eight screenshots, two videos, safe summaries, and the "
            "Playwright report are sealed here.\n\n"
            "Only the two criteria listed in `acceptance.json` are accepted. No provider "
            "call was made while building this checkpoint.\n"
        ),
    )
    return destination


def main() -> int:
    root = build_checkpoint(
        destination=ROOT,
        ui_root=UI_ROOT,
        main_db=MAIN_DB,
        sink_db=SINK_DB,
        econ_db=ECON_DB,
        request=_request,
    )
    print(json.dumps({"root": str(root), "sealed": EvidenceStore(root).is_sealed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
