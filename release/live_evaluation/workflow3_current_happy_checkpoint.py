"""Seal the provider-free current-runtime Workflow 3 happy-path checkpoint."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from .evidence import AcceptanceCriterion, CorrelationIds, EvidenceStore
from .workflow3_lifecycle_evidence import STATE_ROOT, _request

UI_ROOT = STATE_ROOT / "evidence/workflow3-current-runtime-ui-20260824-2"
ROOT = STATE_ROOT / "evidence/workflow3-current-happy-checkpoint-20260824-1"
MAIN_DB = STATE_ROOT / "zeroth.db"
SINK_DB = STATE_ROOT / "action-sink/actions.sqlite3"
ECON_DB = STATE_ROOT / "econ.db"

DEPLOYMENT = "evaluation-studio-v1-governed-remediation-v2"
GRAPH = "evaluation-studio-v1-governed-remediation@4"
TENANT = "evaluation-studio-v1"
MANIFEST_REF = "evaluation://synthetic-action/v1"
MANIFEST_PATH = "/v1/manifests/evaluation%3A%2F%2Fsynthetic-action%2Fv1"
MANIFEST_REF_SHA256 = sha256(MANIFEST_REF.encode()).hexdigest()

ACCEPTED_CRITERIA = (
    "workflow3.signed-action-sink-registered",
    "workflow3.exactly-one-marker-each",
    "audit.approval-action-linkage",
    "audit.receipts-linked",
)

Request = Callable[..., Any]


@dataclass(frozen=True)
class UISource:
    run_ids: tuple[str, str, str]
    manifest_content_hash: str
    run_attachment: Path
    console_summary: Path
    network_summary: Path
    response_identities: Path
    screenshots: tuple[Path, ...]
    videos: tuple[Path, ...]
    report: Path


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid JSON source: {path.name}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"JSON source is not an object: {path.name}")
    return value


def _one(root: Path, pattern: str) -> Path:
    matches = tuple(sorted(root.glob(pattern)))
    if len(matches) != 1:
        raise RuntimeError(f"expected exactly one safe UI artifact matching {pattern}")
    if matches[0].is_symlink() or not matches[0].is_file():
        raise RuntimeError(f"unsafe UI artifact: {matches[0].name}")
    return matches[0]


def _load_ui_source(root: Path) -> UISource:
    root = root.resolve(strict=True)
    results = _load_object(root / "results.json")
    criteria = results.get("criteria")
    if results.get("completed") is not True or not isinstance(criteria, list):
        raise RuntimeError("Playwright source did not complete")
    dispositions = {
        row.get("criterion_id"): row.get("status") for row in criteria if isinstance(row, dict)
    }
    if dispositions != {criterion: "pass" for criterion in ACCEPTED_CRITERIA}:
        raise RuntimeError("Playwright source criteria do not match the checkpoint allowlist")

    indexed = root / "indexed"
    run_attachment = _one(indexed, "*workflow3-current-happy-runs.json")
    attachment = _load_object(run_attachment)
    runs = attachment.get("runs")
    if not isinstance(runs, list) or len(runs) != 3:
        raise RuntimeError("safe run attachment must contain exactly three runs")
    ordered = sorted(runs, key=lambda row: row.get("repetition") if isinstance(row, dict) else -1)
    if [row.get("repetition") for row in ordered if isinstance(row, dict)] != [1, 2, 3]:
        raise RuntimeError("safe run attachment repetitions must be 1, 2, and 3")
    run_ids = tuple(row.get("run_id") for row in ordered if isinstance(row, dict))
    if (
        len(run_ids) != 3
        or len(set(run_ids)) != 3
        or not all(isinstance(value, str) and value for value in run_ids)
    ):
        raise RuntimeError("safe run attachment must contain three distinct run IDs")
    if (
        attachment.get("deployment_ref") != DEPLOYMENT
        or attachment.get("graph_version_ref") != GRAPH
    ):
        raise RuntimeError("safe run attachment targets the wrong deployment or graph")
    manifest_content_hash = attachment.get("manifest_content_hash")
    if not isinstance(manifest_content_hash, str) or len(manifest_content_hash) != 64:
        raise RuntimeError("safe run attachment is missing the manifest content hash")

    screenshots = tuple(sorted(indexed.glob("*.png")))
    if len(screenshots) != 12:
        raise RuntimeError("Playwright source must contain exactly 12 screenshots")
    videos = tuple(sorted(indexed.glob("*.webm")))
    if not videos:
        raise RuntimeError("Playwright source is missing video evidence")
    return UISource(
        run_ids=run_ids,  # type: ignore[arg-type]
        manifest_content_hash=manifest_content_hash,
        run_attachment=run_attachment,
        console_summary=_one(indexed, "*sanitized-console.json"),
        network_summary=_one(indexed, "*sanitized-network.json"),
        response_identities=_one(indexed, "*response-identities.json"),
        screenshots=screenshots,
        videos=videos,
        report=_one(root / "html-report", "index.html"),
    )


def _json_text(value: object, *, label: str) -> dict[str, Any]:
    if not isinstance(value, str):
        raise RuntimeError(f"missing {label}")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid {label}") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError(f"invalid {label}")
    return decoded


def _readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _receipt_hash(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError("missing action receipt")
    return sha256(value.encode()).hexdigest()


def _audit_summary(row: sqlite3.Row) -> dict[str, Any]:
    record = _json_text(row["record_json"], label="audit record")
    metadata = record.get("execution_metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    return {
        "audit_id": row["audit_id"],
        "node_id": row["node_id"],
        "chain_sequence": row["chain_sequence"],
        "record_digest": record.get("record_digest"),
        "previous_record_digest": record.get("previous_record_digest"),
        "record_signature_present": bool(record.get("record_signature")),
        "signing_key_id": record.get("signing_key_id"),
        "signing_algorithm": record.get("signing_algorithm"),
        "cost_usd": float(row["cost_usd"] or 0.0),
        "cost_event_id": row["cost_event_id"],
        "operation_key": metadata.get("operation_key"),
        "manifest_ref_sha256": metadata.get("manifest_ref_sha256"),
        "operation_state": metadata.get("operation_state"),
        "decision": metadata.get("decision"),
    }


def _collect_run_proof(
    run_id: str,
    *,
    request: Request,
    main_db: Path,
    sink_db: Path,
    econ_db: Path,
    manifest: Mapping[str, Any],
    manifest_runs: Mapping[str, Any],
) -> dict[str, Any]:
    runtime = request(f"/v1/runs/{run_id}")
    evidence = request(f"/v1/runs/{run_id}/evidence")
    verification = request(f"/v1/runs/{run_id}/verify-chain", method="POST")
    if not all(isinstance(value, dict) for value in (runtime, evidence, verification)):
        raise RuntimeError(f"runtime evidence shape is invalid for {run_id}")
    output = runtime.get("terminal_output")
    if not isinstance(output, dict):
        raise RuntimeError(f"runtime output is missing for {run_id}")

    with _readonly(main_db) as database:
        run_rows = database.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchall()
        approval_rows = database.execute(
            "SELECT * FROM approvals WHERE run_id = ?", (run_id,)
        ).fetchall()
        operation_rows = database.execute(
            "SELECT * FROM side_effect_operations WHERE run_id = ?", (run_id,)
        ).fetchall()
        audit_rows = database.execute(
            "SELECT * FROM node_audits WHERE run_id = ? ORDER BY chain_sequence", (run_id,)
        ).fetchall()
    if len(run_rows) != 1:
        raise RuntimeError(f"expected exactly one persistent run row for {run_id}")
    run_row = run_rows[0]
    final_output = _json_text(run_row["final_output"], label="persistent final output")

    approval: dict[str, Any] = {"count": len(approval_rows)}
    if len(approval_rows) == 1:
        approval_record = _json_text(approval_rows[0]["record_json"], label="approval record")
        resolution = approval_record.get("resolution")
        resolution = resolution if isinstance(resolution, dict) else {}
        approval.update(
            approval_id=approval_rows[0]["approval_id"],
            status=approval_rows[0]["status"],
            decision=resolution.get("decision"),
        )

    operation: dict[str, Any] = {"count": len(operation_rows)}
    operation_key = output.get("operation_key")
    if len(operation_rows) == 1:
        row = operation_rows[0]
        operation_receipt = _json_text(row["receipt"], label="operation receipt")
        operation.update(
            operation_key=row["operation_key"],
            target_ref_sha256=sha256(str(row["target_ref"]).encode()).hexdigest(),
            state=row["state"],
            attempt=row["attempt"],
            reconciliation_attempts=row["reconciliation_attempts"],
            payload_hash=operation_receipt.get("payload_hash"),
            receipt_sha256=_receipt_hash(operation_receipt.get("receipt")),
            output_matches=(operation_receipt == final_output == output),
        )

    with _readonly(sink_db) as sink:
        marker_rows = sink.execute(
            "SELECT operation_key, payload_hash, receipt "
            "FROM action_markers WHERE operation_key = ?",
            (operation_key,),
        ).fetchall()
    marker: dict[str, Any] = {"count": len(marker_rows)}
    if len(marker_rows) == 1:
        marker.update(
            operation_key=marker_rows[0]["operation_key"],
            payload_hash=marker_rows[0]["payload_hash"],
            receipt_sha256=_receipt_hash(marker_rows[0]["receipt"]),
        )

    with _readonly(econ_db) as economics:
        execution_rows = economics.execute(
            """
            SELECT token_cost_usd, tool_cost_usd, compute_cost_usd, provider_request_id
            FROM execution_events
            WHERE tenant_id = ? AND (execution_id = ? OR join_key = ?)
            """,
            (TENANT, run_id, run_id),
        ).fetchall()
        reservation_rows = economics.execute(
            """
            SELECT actual_cost_usd, provider_request_id
            FROM cost_reservations WHERE tenant_id = ? AND run_id = ?
            """,
            (TENANT, run_id),
        ).fetchall()
    audits = [_audit_summary(row) for row in audit_rows]
    api_audits = evidence.get("audits")
    api_approvals = evidence.get("approvals")
    api_digest_map = (
        {
            row.get("audit_id"): row.get("record_digest")
            for row in api_audits
            if isinstance(row, dict)
        }
        if isinstance(api_audits, list)
        else {}
    )
    database_digest_map = {row["audit_id"]: row["record_digest"] for row in audits}
    api_approval_ids = (
        {row.get("approval_id") for row in api_approvals if isinstance(row, dict)}
        if isinstance(api_approvals, list)
        else set()
    )
    manifest_links = manifest_runs.get("runs")
    run_linked = any(
        isinstance(row, dict)
        and row.get("run_id") == run_id
        and row.get("node_id") == "synthetic-action"
        and row.get("status") == "completed"
        for row in manifest_links
        if isinstance(manifest_links, list)
    )
    return {
        "run_id": run_id,
        "runtime": {
            "status": runtime.get("status"),
            "deployment_ref": runtime.get("deployment_ref"),
            "graph_version_ref": runtime.get("graph_version_ref"),
            "tenant_id": runtime.get("tenant_id"),
            "campaign_id": runtime.get("campaign_id"),
            "persistent_status": run_row["status"],
            "persistent_identity_matches": (
                run_row["deployment_ref"] == runtime.get("deployment_ref")
                and run_row["graph_version_ref"] == runtime.get("graph_version_ref")
                and run_row["tenant_id"] == runtime.get("tenant_id")
            ),
            "operation_key": operation_key,
            "payload_hash": output.get("payload_hash"),
            "receipt_sha256": _receipt_hash(output.get("receipt")),
        },
        "verification": {
            key: verification.get(key)
            for key in ("verified", "signature_verified", "record_count", "unsigned_record_count")
        },
        "approval": approval,
        "audits": audits,
        "runtime_evidence_matches_database": (
            api_digest_map == database_digest_map
            and approval.get("approval_id") in api_approval_ids
        ),
        "operation": operation,
        "marker": marker,
        "manifest": {
            "kind": manifest.get("kind"),
            "side_effect": manifest.get("side_effect"),
            "execution_placement": manifest.get("execution_placement"),
            "content_hash": manifest.get("content_hash"),
            "manifest_ref_sha256": MANIFEST_REF_SHA256,
            "run_linked": run_linked,
        },
        "economics": {
            "execution_event_count": len(execution_rows),
            "reservation_count": len(reservation_rows),
            "execution_cost_usd": sum(
                float(row["token_cost_usd"] or 0)
                + float(row["tool_cost_usd"] or 0)
                + float(row["compute_cost_usd"] or 0)
                for row in execution_rows
            ),
            "reservation_actual_cost_usd": sum(
                float(row["actual_cost_usd"] or 0) for row in reservation_rows
            ),
            "audit_cost_usd": sum(float(row["cost_usd"] or 0) for row in audit_rows),
            "provider_identity_count": sum(
                row["provider_request_id"] is not None
                for row in (*execution_rows, *reservation_rows)
            ),
        },
    }


def _validate_run_proof(proof: Mapping[str, Any]) -> None:
    run_id = proof.get("run_id")
    runtime = proof.get("runtime")
    verification = proof.get("verification")
    approval = proof.get("approval")
    audits = proof.get("audits")
    operation = proof.get("operation")
    marker = proof.get("marker")
    manifest = proof.get("manifest")
    economics = proof.get("economics")
    if not all(
        isinstance(value, dict)
        for value in (runtime, verification, approval, operation, marker, manifest, economics)
    ) or not isinstance(audits, list):
        raise RuntimeError(f"incomplete Workflow 3 proof for {run_id}")
    if (
        runtime.get("status") != "succeeded"
        or runtime.get("deployment_ref") != DEPLOYMENT
        or runtime.get("graph_version_ref") != GRAPH
        or runtime.get("tenant_id") != TENANT
        or runtime.get("campaign_id") != TENANT
        or runtime.get("persistent_status", "COMPLETED") != "COMPLETED"
        or runtime.get("persistent_identity_matches", True) is not True
    ):
        raise RuntimeError(f"runtime/persistence identity mismatch for {run_id}")
    if (
        approval.get("count") != 1
        or approval.get("status") != "resolved"
        or approval.get("decision") != "approve"
    ):
        raise RuntimeError(f"approval is not a single approved resolution for {run_id}")
    if operation.get("count") != 1:
        raise RuntimeError(f"expected exactly one operation row for {run_id}")
    if marker.get("count") != 1:
        raise RuntimeError(f"expected exactly one action marker for {run_id}")
    identity = runtime.get("operation_key")
    payload_hash = runtime.get("payload_hash")
    receipt_sha256 = runtime.get("receipt_sha256")
    if (
        identity != operation.get("operation_key")
        or identity != marker.get("operation_key")
        or payload_hash != operation.get("payload_hash")
        or payload_hash != marker.get("payload_hash")
        or receipt_sha256 != operation.get("receipt_sha256")
        or receipt_sha256 != marker.get("receipt_sha256")
        or operation.get("output_matches", True) is not True
        or operation.get("target_ref_sha256", MANIFEST_REF_SHA256) != MANIFEST_REF_SHA256
        or operation.get("state") != "COMPLETED"
    ):
        raise RuntimeError(f"operation, receipt, or payload linkage failed for {run_id}")
    if (
        verification.get("verified") is not True
        or verification.get("signature_verified") is not True
        or verification.get("unsigned_record_count") != 0
        or verification.get("record_count") != len(audits)
        or not audits
    ):
        raise RuntimeError(f"signed audit verification failed for {run_id}")
    sequences = [row.get("chain_sequence") for row in audits if isinstance(row, dict)]
    if sequences != list(range(1, len(audits) + 1)):
        raise RuntimeError(f"audit digest chain sequence failed for {run_id}")
    for index, row in enumerate(audits):
        if not isinstance(row, dict) or row.get("record_signature_present") is not True:
            raise RuntimeError(f"audit record is unsigned for {run_id}")
        if index and row.get("previous_record_digest") != audits[index - 1].get("record_digest"):
            raise RuntimeError(f"audit digest chain is broken for {run_id}")
    action_rows = [
        row for row in audits if isinstance(row, dict) and row.get("node_id") == "synthetic-action"
    ]
    approval_rows = [
        row for row in audits if isinstance(row, dict) and row.get("node_id") == "approval"
    ]
    if (
        len(action_rows) != 1
        or not approval_rows
        or max(row["chain_sequence"] for row in approval_rows) >= action_rows[0]["chain_sequence"]
        or action_rows[0].get("operation_key") != identity
        or action_rows[0].get("manifest_ref_sha256") != MANIFEST_REF_SHA256
        # Persistence stores the lifecycle enum in uppercase while the signed
        # audit vocabulary deliberately emits the normalized lowercase value.
        or action_rows[0].get("operation_state") != "completed"
    ):
        raise RuntimeError(f"approval/action audit linkage failed for {run_id}")
    if proof.get("runtime_evidence_matches_database", True) is not True:
        raise RuntimeError(f"runtime audit evidence does not match persistence for {run_id}")
    if (
        manifest.get("side_effect") is not True
        or manifest.get("execution_placement") != "local_only"
        or manifest.get("manifest_ref_sha256") != MANIFEST_REF_SHA256
        or manifest.get("run_linked") is not True
    ):
        raise RuntimeError(f"manifest linkage failed for {run_id}")
    cost_values = (
        economics.get("execution_cost_usd"),
        economics.get("reservation_actual_cost_usd"),
        economics.get("audit_cost_usd"),
    )
    if economics.get("provider_identity_count") != 0 or any(value != 0.0 for value in cost_values):
        raise RuntimeError(f"zero provider/economics cost invariant failed for {run_id}")


def _ingest_ui(store: EvidenceStore, source: UISource) -> list[str]:
    paths: list[str] = []
    mappings = (
        (source.run_attachment, Path("console") / source.run_attachment.name),
        (source.console_summary, Path("console") / source.console_summary.name),
        (source.response_identities, Path("console") / source.response_identities.name),
        (source.network_summary, Path("network") / source.network_summary.name),
        (source.report, Path("playwright-report/index.html")),
        *((path, Path("screenshots") / path.name) for path in source.screenshots),
        *((path, Path("videos") / path.name) for path in source.videos),
    )
    for original, relative in mappings:
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
    source = _load_ui_source(ui_root)
    readiness = request("/audit-readiness")
    manifest = request(MANIFEST_PATH)
    manifest_runs = request(f"{MANIFEST_PATH}/runs")
    if not all(isinstance(value, dict) for value in (readiness, manifest, manifest_runs)):
        raise RuntimeError("runtime readiness or manifest response is invalid")
    if readiness.get("ready") is not True or readiness.get("state") != "signed":
        raise RuntimeError("runtime audit readiness is not signed")
    if (
        manifest.get("content_hash") != source.manifest_content_hash
        or manifest.get("side_effect") is not True
        or manifest.get("execution_placement") != "local_only"
    ):
        raise RuntimeError("registered action manifest does not match the UI attachment")
    proofs = [
        _collect_run_proof(
            run_id,
            request=request,
            main_db=main_db,
            sink_db=sink_db,
            econ_db=econ_db,
            manifest=manifest,
            manifest_runs=manifest_runs,
        )
        for run_id in source.run_ids
    ]
    for proof in proofs:
        _validate_run_proof(proof)

    store = EvidenceStore(destination)
    store.write_manifest(
        {
            "schema_version": 1,
            "checkpoint": "workflow3-current-provider-free-happy-path",
            "created_at": datetime.now(UTC).isoformat(),
            "source_root": ui_root.name,
            "source_results_sha256": sha256((ui_root / "results.json").read_bytes()).hexdigest(),
            "run_ids": list(source.run_ids),
            "deployment_ref": DEPLOYMENT,
            "graph_version_ref": GRAPH,
            "tenant_id": TENANT,
            "provider_calls_performed": 0,
            "accepted_criteria": list(ACCEPTED_CRITERIA),
        }
    )
    platform_path = Path("runtime/platform.json")
    store._write_exclusive(
        platform_path,
        {
            "audit_ready": readiness.get("ready"),
            "audit_state": readiness.get("state"),
            "signing_required": readiness.get("signing_required"),
            "signer_available": readiness.get("signer_available"),
            "manifest": {
                "kind": manifest.get("kind"),
                "side_effect": manifest.get("side_effect"),
                "execution_placement": manifest.get("execution_placement"),
                "content_hash": manifest.get("content_hash"),
                "manifest_ref_sha256": MANIFEST_REF_SHA256,
            },
        },
    )
    proof_paths: list[str] = []
    for index, proof in enumerate(proofs, start=1):
        relative = Path(f"runtime/run-{index}.json")
        store._write_exclusive(relative, proof)
        proof_paths.append(relative.as_posix())
        action_audit = next(row for row in proof["audits"] if row["node_id"] == "synthetic-action")
        store.append_event(
            "campaign.operation.run.audit.verified",
            {
                "repetition": index,
                "result": "pass",
                "proof_path": relative.as_posix(),
            },
            correlation=CorrelationIds(
                operation_id=proof["runtime"]["operation_key"],
                run_id=proof["run_id"],
                audit_event_id=action_audit["audit_id"],
            ),
        )
    ui_paths = _ingest_ui(store, source)
    common = tuple([platform_path.as_posix(), *proof_paths, *ui_paths])
    acceptance = tuple(
        AcceptanceCriterion(criterion, "pass", common) for criterion in ACCEPTED_CRITERIA
    )
    store.finalize_bundle(
        acceptance=acceptance,
        report_markdown=(
            "# Workflow 3 current happy-path checkpoint\n\n"
            "Three UI-submitted approval runs were independently rejoined to the current "
            "runtime, primary persistence database, campaign action-sink database, and "
            "economics database. Each run has one approved resolution, one completed "
            "side-effect operation, one authoritative sink marker, matching receipt and "
            "payload hashes, a verified signed digest chain, manifest linkage, and zero "
            "provider/economics cost. The 12 sanitized screenshots, safe console/network "
            "summaries, videos, and Playwright report are sealed with the runtime proof.\n\n"
            "Only the four criteria listed in `acceptance.json` are accepted. No provider "
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
