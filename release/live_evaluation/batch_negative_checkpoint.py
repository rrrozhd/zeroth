"""Fail-closed sealer for the provider-free batch negative/recovery campaign."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .control_plane import dirty_tree_hash
from .evidence import AcceptanceCriterion, CorrelationIds, EvidenceStore
from .provider_free_composed import ITEMS, ProviderFreeComposedFixture, read_fixture_manifest

CONTRACT_CRITERIA = {
    "batching.malformed-item",
    "workflow2.negative-empty-batch",
    "workflow2.negative-over-24-batch",
}
RUNTIME_CRITERIA = {"batching.active-refresh-restoration", "runs.cancel"}
EXPECTED_SCREENSHOTS = {
    "contract": {
        "batch-empty-configured.png",
        "batch-empty-rejected.png",
        "batch-over-24-configured.png",
        "batch-over-24-rejected.png",
        "batch-malformed-item-configured.png",
        "batch-malformed-item-rejected.png",
        "batch-contract-rejections-refresh-restored.png",
    },
    "runtime": {
        "batch-active-before-refresh.png",
        "batch-active-refresh-restored.png",
        "batch-cancel-configured.png",
        "batch-cancelled-refresh-restored.png",
    },
}


def _object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid {label}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"invalid {label}")
    return value


def _array(path: Path, *, label: str) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid {label}") from exc
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise RuntimeError(f"invalid {label}")
    return value


def _safe_source(root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        raise RuntimeError("browser artifact source escapes its evidence root")
    source = root / candidate
    if source.is_symlink() or not source.is_file():
        raise RuntimeError(f"browser artifact is missing or not regular: {relative}")
    resolved = source.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise RuntimeError("browser artifact source escapes its evidence root")
    return resolved


def _load_browser_root(
    root: Path,
    *,
    label: str,
    expected_criteria: set[str],
    summary_suffix: str,
) -> tuple[dict[str, Any], dict[str, Path], list[Path]]:
    results = _object(root / "results.json", label=f"{label} browser results")
    criteria = results.get("criteria")
    if results.get("schema_version") != 1 or results.get("completed") is not True:
        raise RuntimeError(f"{label} browser run did not complete")
    if not isinstance(criteria, list) or any(not isinstance(row, dict) for row in criteria):
        raise RuntimeError(f"{label} browser criteria are invalid")
    statuses = {str(row.get("criterion_id")): row.get("status") for row in criteria}
    if set(statuses) != expected_criteria or any(status != "pass" for status in statuses.values()):
        raise RuntimeError(f"{label} browser root must contain exact criteria and all must pass")

    rows = results.get("artifacts")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RuntimeError(f"{label} browser artifact index is invalid")
    artifacts: dict[str, Path] = {}
    for row in rows:
        source_name = row.get("source")
        destination_name = row.get("destination")
        if not isinstance(source_name, str) or not isinstance(destination_name, str):
            raise RuntimeError(f"{label} browser artifact index is invalid")
        destination = Path(destination_name)
        if (
            destination.is_absolute()
            or ".." in destination.parts
            or len(destination.parts) < 2
            or destination_name in artifacts
        ):
            raise RuntimeError(f"{label} browser artifact destination is invalid")
        artifacts[destination_name] = _safe_source(root, source_name)

    suffix_matches = [name for name in artifacts if name.endswith(summary_suffix)]
    screenshots = {
        Path(name).name for name in artifacts if Path(name).parts[0] == "screenshots"
    }
    videos = [name for name in artifacts if Path(name).parts[0] == "videos"]
    if (
        len(suffix_matches) != 1
        or any(
            sum(name.endswith(expected) for name in screenshots) != 1
            for expected in EXPECTED_SCREENSHOTS[label]
        )
        or len(screenshots) != len(EXPECTED_SCREENSHOTS[label])
        or not videos
        or sum(name.endswith("sanitized-console.json") for name in artifacts) != 1
        or sum(name.endswith("sanitized-network.json") for name in artifacts) != 1
        or "playwright-report/index.html" not in artifacts
    ):
        raise RuntimeError(f"{label} browser evidence is incomplete")

    html_root = root / "html-report"
    if html_root.is_symlink() or not (html_root / "index.html").is_file():
        raise RuntimeError(f"{label} HTML report is missing")
    html_files = sorted(path for path in html_root.rglob("*") if path.is_file())
    if not any(path.parent != html_root for path in html_files):
        raise RuntimeError(f"{label} HTML report data is missing")
    if any(
        path.is_symlink() or not path.resolve().is_relative_to(html_root.resolve())
        for path in html_files
    ):
        raise RuntimeError(f"{label} HTML report contains unsafe files")
    summary = _object(artifacts[suffix_matches[0]], label=f"{label} summary")
    return summary, artifacts, html_files


def _validate_contract(summary: Mapping[str, Any], artifacts: Mapping[str, Path]) -> dict[str, Any]:
    observations = summary.get("observations")
    if not isinstance(observations, list) or any(not isinstance(row, dict) for row in observations):
        raise RuntimeError("contract observations are invalid")
    observed = {
        str(row.get("id")): (row.get("status"), row.get("validation_type"))
        for row in observations
    }
    expected = {
        "empty": (422, "too_short"),
        "over-24": (422, "too_long"),
        "malformed-item": (422, "missing"),
    }
    if observed != expected:
        raise RuntimeError("contract rejections do not have the exact 422 validation types")
    before = summary.get("run_count_before")
    after = summary.get("run_count_after")
    if (
        not isinstance(before, int)
        or before != after
        or summary.get("run_identities_unchanged") is not True
        or summary.get("tenant_cost_unchanged") is not True
    ):
        raise RuntimeError("contract rejection produced run or cost side effects")
    if (
        summary.get("expected_validation_console_errors") != 3
        or summary.get("unexpected_console_errors") != 0
        or summary.get("page_errors") != 0
        or summary.get("provider_calls_performed") != 0
    ):
        raise RuntimeError("contract rejection console/provider accounting is invalid")

    console_path = next(
        path for name, path in artifacts.items() if name.endswith("sanitized-console.json")
    )
    console = _array(console_path, label="contract console")
    errors = [row for row in console if row.get("type") == "error"]
    if len(errors) != 3 or any(not str(row.get("url", "")).endswith("/v1/runs") for row in errors):
        raise RuntimeError("contract console must contain exactly three expected validation errors")
    network_path = next(
        path for name, path in artifacts.items() if name.endswith("sanitized-network.json")
    )
    network = _object(network_path, label="contract network")
    responses = network.get("responses")
    if not isinstance(responses, list) or any(not isinstance(row, dict) for row in responses):
        raise RuntimeError("contract network is invalid")
    failures = [
        row
        for row in responses
        if isinstance(row.get("status"), int) and row["status"] >= 400
    ]
    if (
        len(failures) != 3
        or any(
            row.get("status") != 422
            or row.get("resource_type") != "fetch"
            or not str(row.get("url", "")).endswith("/v1/runs")
            for row in failures
        )
    ):
        raise RuntimeError("contract network must contain exactly three POST validation responses")
    return {
        "validation_types": {key: value[1] for key, value in expected.items()},
        "run_count_before": before,
        "run_count_after": after,
        "cost_side_effects": 0,
        "run_side_effects": 0,
        "expected_console_errors": 3,
        "unexpected_console_errors": 0,
        "provider_calls_performed": 0,
    }


def _validate_runtime(
    summary: Mapping[str, Any], artifacts: Mapping[str, Path], fixture: ProviderFreeComposedFixture
) -> dict[str, Any]:
    health = summary.get("health")
    active = summary.get("active_refresh")
    cancellation = summary.get("cancellation")
    if (
        not isinstance(health, Mapping)
        or health.get("status") != "ok"
        or health.get("deployment_ref") != fixture.parent_deployment_ref
        or health.get("graph_version_ref") != fixture.parent_graph_version_ref
        or not isinstance(active, Mapping)
        or not isinstance(cancellation, Mapping)
    ):
        raise RuntimeError("runtime health or observations are invalid")
    active_run_id = active.get("run_id")
    cancel_run_id = cancellation.get("run_id")
    if (
        not isinstance(active_run_id, str)
        or not active_run_id
        or not isinstance(cancel_run_id, str)
        or not cancel_run_id
        or active_run_id == cancel_run_id
        or active.get("terminal_status") != "succeeded"
        or active.get("restored_while_active") is not True
        or active.get("audit_count") != 9
        or cancellation.get("terminal_status") != "failed"
        or cancellation.get("failure_reason") != "operator_cancelled"
        or cancellation.get("child_count") != 4
        or cancellation.get("child_statuses") != ["failed"] * 4
        or cancellation.get("child_identities_stable_after_refresh") is not True
        or cancellation.get("audit_count") != 1
        or summary.get("provider_calls_performed") != 0
    ):
        raise RuntimeError("runtime active/refresh/cancellation observations are invalid")
    console_path = next(
        path for name, path in artifacts.items() if name.endswith("sanitized-console.json")
    )
    console = _array(console_path, label="runtime console")
    if any(row.get("type") == "error" for row in console):
        raise RuntimeError("runtime browser console contains unexpected errors")
    network_path = next(
        path for name, path in artifacts.items() if name.endswith("sanitized-network.json")
    )
    network = _object(network_path, label="runtime network")
    responses = network.get("responses")
    if not isinstance(responses, list) or any(not isinstance(row, dict) for row in responses):
        raise RuntimeError("runtime network is invalid")
    if any(isinstance(row.get("status"), int) and row["status"] >= 400 for row in responses):
        raise RuntimeError("runtime network contains unexpected failures")
    return {
        "active_run_id": active_run_id,
        "active_terminal_status": "succeeded",
        "active_refresh_restored": True,
        "active_audit_count": 9,
        "cancel_run_id": cancel_run_id,
        "cancel_terminal_status": "failed",
        "cancel_reason": "operator_cancelled",
        "cancel_child_count": 4,
        "cancel_audit_count": 1,
        "provider_calls_performed": 0,
    }


def _snapshot_attestation(path: Path) -> dict[str, object]:
    path = path.expanduser().resolve(strict=True)
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("SQLite snapshot must be a regular immutable file")
    if any(path.with_name(f"{path.name}{suffix}").exists() for suffix in ("-wal", "-shm")):
        raise RuntimeError("SQLite snapshot has mutable sidecar files")
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
        try:
            row = connection.execute("PRAGMA quick_check").fetchone()
        finally:
            connection.close()
    except sqlite3.DatabaseError as exc:
        raise RuntimeError("SQLite snapshot is invalid") from exc
    if row != ("ok",):
        raise RuntimeError("SQLite snapshot quick_check failed")
    return {
        "filename": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size_bytes": path.stat().st_size,
        "quick_check": "ok",
    }


def _decode_object(value: object, *, label: str) -> dict[str, Any]:
    if not isinstance(value, str):
        raise RuntimeError(f"{label} must be JSON")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{label} must be JSON") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError(f"{label} must be a JSON object")
    return decoded


def _canonical_graph_ref(value: object) -> str:
    """Normalize the runtime child-run spelling to the published graph identity."""
    text = str(value)
    prefix, separator, version = text.rpartition(":v")
    return f"{prefix}@{version}" if separator and version.isdigit() else text


def _validate_audits(
    connection: sqlite3.Connection,
    *,
    rows: Sequence[sqlite3.Row],
    expected_parent_counts: Mapping[str, int],
) -> tuple[int, dict[str, int]]:
    total = 0
    counts: dict[str, int] = {}
    for run in rows:
        audits = connection.execute(
            "SELECT audit_id,run_id,thread_id,graph_version_ref,deployment_ref,tenant_id,"
            "workspace_id,record_json,cost_usd,cost_event_id,chain_sequence "
            "FROM node_audits WHERE run_id=? ORDER BY chain_sequence",
            (run["run_id"],),
        ).fetchall()
        if not audits:
            raise RuntimeError("every involved run must have signed audit records")
        counts[str(run["run_id"])] = len(audits)
        expected_count = expected_parent_counts.get(str(run["run_id"]))
        if expected_count is not None and len(audits) != expected_count:
            raise RuntimeError("runtime and SQLite signed audit counts do not match")
        previous_digest: str | None = None
        for expected_sequence, audit in enumerate(audits, start=1):
            record = _decode_object(audit["record_json"], label="audit record")
            identity_fields = {
                "audit_id": audit["audit_id"],
                "run_id": run["run_id"],
                "thread_id": run["thread_id"],
                "graph_version_ref": run["graph_version_ref"],
                "deployment_ref": run["deployment_ref"],
                "tenant_id": run["tenant_id"],
                "workspace_id": run["workspace_id"],
            }
            if any(record.get(key) != value for key, value in identity_fields.items()):
                raise RuntimeError("audit correlation does not match its persisted run")
            if (
                audit["chain_sequence"] != expected_sequence
                or not isinstance(record.get("record_signature"), str)
                or not record["record_signature"]
                or not isinstance(record.get("record_digest"), str)
                or not record["record_digest"]
                or not isinstance(record.get("signing_key_id"), str)
                or not record["signing_key_id"]
                or not isinstance(record.get("signing_algorithm"), str)
                or not record["signing_algorithm"]
                or record.get("previous_record_digest") != previous_digest
            ):
                raise RuntimeError("every involved audit record must be signed in an intact chain")
            if (
                audit["cost_usd"] not in (None, 0, 0.0)
                or audit["cost_event_id"] is not None
                or record.get("cost_usd") not in (None, 0, 0.0)
                or record.get("estimated_cost_usd") not in (None, 0, 0.0)
                or record.get("cost_event_id") is not None
                or record.get("token_usage") is not None
            ):
                raise RuntimeError("provider-free batch audits must have zero cost/provider usage")
            previous_digest = record["record_digest"]
            total += 1
    return total, counts


def _validate_sqlite(
    *,
    pre_snapshot: Path,
    post_snapshot: Path,
    fixture: ProviderFreeComposedFixture,
    runtime: Mapping[str, Any],
    tenant_id: str,
) -> dict[str, Any]:
    active_id = str(runtime["active_run_id"])
    cancel_id = str(runtime["cancel_run_id"])
    with sqlite3.connect(f"file:{pre_snapshot.resolve()}?mode=ro&immutable=1", uri=True) as pre:
        present = pre.execute(
            "SELECT COUNT(*) FROM runs WHERE tenant_id=? AND run_id IN (?,?)",
            (tenant_id, active_id, cancel_id),
        ).fetchone()[0]
    if present != 0:
        raise RuntimeError("batch recovery runs already exist in the pre snapshot")

    connection = sqlite3.connect(
        f"file:{post_snapshot.resolve()}?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    try:
        parents = connection.execute(
            "SELECT * FROM runs WHERE tenant_id=? AND run_id IN (?,?) ORDER BY run_id",
            (tenant_id, active_id, cancel_id),
        ).fetchall()
        if len(parents) != 2:
            raise RuntimeError("post snapshot does not contain both exact parent runs")
        by_id = {str(row["run_id"]): row for row in parents}
        active = by_id[active_id]
        cancelled = by_id[cancel_id]
        if (
            active["parent_run_id"] is not None
            or active["thread_id"] != active_id
            or active["deployment_ref"] != fixture.parent_deployment_ref
            or active["graph_version_ref"] != fixture.parent_graph_version_ref
            or active["status"] != "COMPLETED"
            or _decode_object(active["final_output"], label="active final output")
            != {"items": list(ITEMS)}
            or cancelled["parent_run_id"] is not None
            or cancelled["thread_id"] != active_id
            or cancelled["deployment_ref"] != fixture.parent_deployment_ref
            or cancelled["graph_version_ref"] != fixture.parent_graph_version_ref
            or cancelled["status"] != "FAILED"
            or _decode_object(
                cancelled["failure_state"], label="cancellation failure"
            ).get("reason")
            != "operator_cancelled"
        ):
            raise RuntimeError("post snapshot parent correlations are invalid")

        active_children = connection.execute(
            "SELECT * FROM runs WHERE tenant_id=? AND parent_run_id=? ORDER BY run_id",
            (tenant_id, active_id),
        ).fetchall()
        cancelled_children = connection.execute(
            "SELECT * FROM runs WHERE tenant_id=? AND parent_run_id=? ORDER BY run_id",
            (tenant_id, cancel_id),
        ).fetchall()
        all_children = [*active_children, *cancelled_children]
        child_ids = [str(row["run_id"]) for row in all_children]
        child_threads = [str(row["thread_id"]) for row in all_children]
        child_graphs = {_canonical_graph_ref(row["graph_version_ref"]) for row in all_children}
        if (
            len(active_children) != 8
            or len(cancelled_children) != 4
            or len(set(child_ids)) != 12
            or len(set(child_threads)) != 12
            or any(row["status"] != "COMPLETED" for row in active_children)
            or any(row["status"] != "FAILED" for row in cancelled_children)
            or any(row["deployment_ref"] != fixture.child_deployment_ref for row in all_children)
            or child_graphs != {fixture.child_graph_version_ref}
            or any(
                row["lease_worker_id"] is not None
                or row["lease_acquired_at"] is not None
                or row["lease_expires_at"] is not None
                for row in all_children
            )
        ):
            raise RuntimeError(
                "post snapshot child identities are not unique terminal correlations"
            )
        outputs = sorted(
            (
                _decode_object(row["final_output"], label="child final output")
                for row in active_children
            ),
            key=lambda item: item.get("index", -1),
        )
        if outputs != list(ITEMS):
            raise RuntimeError("active batch child outputs are incomplete or unordered")
        if any(
            _decode_object(row["failure_state"], label="cancelled child failure").get("reason")
            != "operator_cancelled"
            for row in cancelled_children
        ):
            raise RuntimeError("cancelled children did not persist operator_cancelled")
        for row in all_children:
            metadata = _decode_object(row["metadata"], label="run metadata")
            if metadata.get("total_cost_usd", 0.0) not in (0, 0.0) or metadata.get(
                "total_estimated_cost_usd", 0.0
            ) not in (0, 0.0):
                raise RuntimeError("provider-free child run has non-zero economics")

        involved = [*parents, *all_children]
        audit_total, audit_counts = _validate_audits(
            connection,
            rows=involved,
            expected_parent_counts={active_id: 9, cancel_id: 1},
        )
        cancel_control_nodes = connection.execute(
            "SELECT node_id FROM node_audits WHERE run_id=? ORDER BY chain_sequence",
            (cancel_id,),
        ).fetchall()
        if [row["node_id"] for row in cancel_control_nodes] != ["run.control.cancelled"]:
            raise RuntimeError("cancellation lacks its signed run.control.cancelled audit")
    finally:
        connection.close()
    return {
        "active_run_id": active_id,
        "cancel_run_id": cancel_id,
        "parent_run_count": 2,
        "active_child_count": 8,
        "cancelled_child_count": 4,
        "unique_child_run_count": 12,
        "unique_child_thread_count": 12,
        "child_graph_version_count": len(child_graphs),
        "signed_audit_count": audit_total,
        "active_parent_audit_count": audit_counts[active_id],
        "cancel_parent_audit_count": audit_counts[cancel_id],
        "provider_calls_performed": 0,
        "cost_event_count": 0,
        "attributed_cost_usd": 0.0,
    }


def _repository_identity(repository_root: Path) -> tuple[str, str]:
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return revision, dirty_tree_hash(repository_root).removeprefix("sha256:")


def _ingest_browser_artifacts(
    store: EvidenceStore,
    *,
    label: str,
    root: Path,
    artifacts: Mapping[str, Path],
    html_files: Sequence[Path],
) -> list[str]:
    paths: list[str] = []
    for destination, source in sorted(artifacts.items()):
        original = Path(destination)
        if original.parts[0] == "playwright-report":
            continue
        relative = Path(original.parts[0]) / label / Path(*original.parts[1:])
        store.ingest_artifact(source, relative)
        paths.append(relative.as_posix())
    html_root = root / "html-report"
    for source in html_files:
        relative = Path("playwright-report") / label / source.relative_to(html_root)
        store.ingest_artifact(source, relative)
        paths.append(relative.as_posix())
    results_relative = Path("playwright-report") / label / "results.json"
    store.ingest_artifact(root / "results.json", results_relative)
    paths.append(results_relative.as_posix())
    return paths


def build_checkpoint(
    *,
    contract_root: Path,
    runtime_root: Path,
    pre_snapshot: Path,
    post_snapshot: Path,
    fixture_manifest: Path,
    destination: Path,
    repository_root: Path,
    tenant_id: str,
) -> Path:
    """Validate the exact UI/runtime/database matrix and seal only sanitized evidence."""
    contract_root = contract_root.expanduser().resolve(strict=True)
    runtime_root = runtime_root.expanduser().resolve(strict=True)
    pre_snapshot = pre_snapshot.expanduser().resolve(strict=True)
    post_snapshot = post_snapshot.expanduser().resolve(strict=True)
    fixture_manifest = fixture_manifest.expanduser().resolve(strict=True)
    repository_root = repository_root.expanduser().resolve(strict=True)
    destination = destination.expanduser().resolve(strict=False)
    if destination.exists():
        raise FileExistsError(destination)
    if not tenant_id:
        raise ValueError("tenant_id is required")

    fixture = read_fixture_manifest(fixture_manifest)
    if fixture.provider_calls_performed != 0 or fixture.provider_economics_status != "blocked":
        raise RuntimeError("fixture must remain provider-free with economics blocked")
    contract_summary, contract_artifacts, contract_html = _load_browser_root(
        contract_root,
        label="contract",
        expected_criteria=CONTRACT_CRITERIA,
        summary_suffix="batch-contract-rejection-summary.json",
    )
    runtime_summary, runtime_artifacts, runtime_html = _load_browser_root(
        runtime_root,
        label="runtime",
        expected_criteria=RUNTIME_CRITERIA,
        summary_suffix="batch-runtime-negative-summary.json",
    )
    contract_validation = _validate_contract(contract_summary, contract_artifacts)
    runtime_validation = _validate_runtime(runtime_summary, runtime_artifacts, fixture)
    pre_attestation = _snapshot_attestation(pre_snapshot)
    post_attestation = _snapshot_attestation(post_snapshot)
    sqlite_validation = _validate_sqlite(
        pre_snapshot=pre_snapshot,
        post_snapshot=post_snapshot,
        fixture=fixture,
        runtime=runtime_validation,
        tenant_id=tenant_id,
    )
    revision, diff_sha256 = _repository_identity(repository_root)

    store = EvidenceStore(destination)
    store.write_manifest(
        {
            "schema_version": 1,
            "checkpoint": "batch-negative-recovery-accepted-20260826-2",
            "created_at": datetime.now(UTC).isoformat(),
            "tenant_id": tenant_id,
            "revision": revision,
            "diff_sha256": diff_sha256,
            "fixture": asdict(fixture),
            "active_run_id": runtime_validation["active_run_id"],
            "cancel_run_id": runtime_validation["cancel_run_id"],
            "provider_calls_performed": 0,
            "provider_economics_status": "blocked",
            "raw_snapshots_in_sealed_bundle": False,
        }
    )
    store.ingest_artifact(fixture_manifest, "handoff/provider-free-fixture-manifest.json")
    evidence_paths = ["handoff/provider-free-fixture-manifest.json"]
    evidence_paths.extend(
        _ingest_browser_artifacts(
            store,
            label="contract",
            root=contract_root,
            artifacts=contract_artifacts,
            html_files=contract_html,
        )
    )
    evidence_paths.extend(
        _ingest_browser_artifacts(
            store,
            label="runtime",
            root=runtime_root,
            artifacts=runtime_artifacts,
            html_files=runtime_html,
        )
    )
    constructed = {
        "contract-validation.json": contract_validation,
        "runtime-validation.json": runtime_validation,
        "sqlite-validation.json": sqlite_validation,
        "snapshot-attestations.json": {
            "schema_version": 1,
            "pre": pre_attestation,
            "post": post_attestation,
            "raw_snapshots_in_sealed_bundle": False,
        },
    }
    for name, value in constructed.items():
        relative = Path("reconciliation") / name
        store._write_exclusive(relative, value)
        evidence_paths.append(relative.as_posix())

    contract_event = store.append_event(
        "campaign.ui.batch_negative_contract_verified",
        {
            "result": "pass",
            "validation_case_count": 3,
            "run_side_effects": 0,
            "cost_side_effects": 0,
        },
        correlation=CorrelationIds(ui_action_id="batch-negative-contract-matrix"),
    )
    active_event = store.append_event(
        "campaign.run.batch_negative_active_verified",
        {
            "result": "pass",
            "terminal_status": "succeeded",
            "child_count": 8,
            "signed_audit_count": 9,
        },
        correlation=CorrelationIds(run_id=str(runtime_validation["active_run_id"])),
    )
    cancel_event = store.append_event(
        "campaign.run.batch_negative_cancel_verified",
        {
            "result": "pass",
            "terminal_status": "failed",
            "failure_reason": "operator_cancelled",
            "child_count": 4,
            "signed_audit_count": 1,
        },
        correlation=CorrelationIds(run_id=str(runtime_validation["cancel_run_id"])),
    )
    criterion_evidence = tuple(
        [
            *evidence_paths,
            f"events.ndjson#{contract_event}",
            f"events.ndjson#{active_event}",
            f"events.ndjson#{cancel_event}",
        ]
    )
    acceptance = [
        *(
            AcceptanceCriterion(criterion, "pass", criterion_evidence)
            for criterion in sorted(CONTRACT_CRITERIA | RUNTIME_CRITERIA)
        ),
        AcceptanceCriterion(
            "batching.provider-economics",
            "blocked",
            criterion_evidence,
            "Provider-free fixture: no live model call occurred, so provider economics "
            "cannot pass.",
        ),
    ]
    store.finalize_bundle(
        acceptance=acceptance,
        report_markdown=(
            "# Batch negative and recovery checkpoint\n\n"
            "The exact five deterministic acceptance criteria passed. Empty, over-24, and "
            "malformed-item submissions returned the exact 422 validation types without creating "
            "runs or costs. The active eight-item batch survived refresh and completed with eight "
            "unique terminal children. Operator cancellation ended the parent and four in-flight "
            "children without duplicate identities. Every involved persisted run has a signed, "
            "intact audit chain, and all persisted provider/cost fields are zero.\n\n"
            "Provider economics remains **blocked**, not passed, because this checkpoint "
            "deliberately performed zero provider calls. The pre/post SQLite snapshots are "
            "represented only by SHA-256, byte size, and successful quick-check attestations; "
            "raw databases are excluded.\n"
        ),
    )
    return destination


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract-root", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--pre-snapshot", type=Path, required=True)
    parser.add_argument("--post-snapshot", type=Path, required=True)
    parser.add_argument("--fixture-manifest", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--tenant-id", required=True)
    args = parser.parse_args()
    root = build_checkpoint(
        contract_root=args.contract_root,
        runtime_root=args.runtime_root,
        pre_snapshot=args.pre_snapshot,
        post_snapshot=args.post_snapshot,
        fixture_manifest=args.fixture_manifest,
        destination=args.destination,
        repository_root=args.repository_root,
        tenant_id=args.tenant_id,
    )
    print(json.dumps({"root": str(root), "sealed": EvidenceStore(root).is_sealed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
