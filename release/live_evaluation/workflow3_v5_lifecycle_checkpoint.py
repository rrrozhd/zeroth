"""Seal the exact native-Safari Workflow 3 v5 lifecycle checkpoint."""

from __future__ import annotations

import hashlib
import platform
import re
import sqlite3
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from .evidence import AcceptanceCriterion, CorrelationIds, EvidenceStore
from .runtime_paths import resolve_runtime_paths

_RUNTIME_PATHS = resolve_runtime_paths()
WORKTREE = _RUNTIME_PATHS.worktree
STATE_ROOT = _RUNTIME_PATHS.state_root
NATIVE_ROOT = STATE_ROOT / "evidence/native-safari-studio-20260824-1"
ROOT = STATE_ROOT / "evidence/workflow3-v5-lifecycle-checkpoint-20260824-1"
SERVICE_KEY_PATH = STATE_ROOT / "runtime-secrets/service-api-key"
MAIN_DB = STATE_ROOT / "zeroth.db"
ECON_DB = STATE_ROOT / "econ.db"
SINK_DB = STATE_ROOT / "action-sink/actions.sqlite3"
BASE_URL = "http://127.0.0.1:8122"
TENANT = "evaluation-studio-v1"
WORKFLOW_ID = "evaluation-studio-v1-governed-remediation"
FRESH_DEPLOYMENT = "evaluation-studio-v1-governed-remediation-lifecycle-v5-20260824-1"
FRESH_GRAPH = f"{WORKFLOW_ID}@5"
STABLE_DEPLOYMENT = "evaluation-studio-v1-governed-remediation-v2"
STABLE_GRAPH = f"{WORKFLOW_ID}@4"
RUN_ID = "3959264c97974cb39c3e43035afac293"
APPROVAL_ID = "aa48535adc09473583305aa281c346f5"

ACCEPTED_CRITERIA = (
    "ui.publish-deploy-run",
    "workflow3.publish-deploy-restart",
    "workflow3.negative-sla-expiry",
    "workflow3.health-exact-graph-version",
)

_DIFF_CHANGE_FIELDS = (
    "node_changes",
    "edge_changes",
    "contract_changes",
    "policy_changes",
    "condition_changes",
    "memory_connector_changes",
    "executable_unit_binding_changes",
)
_CREDENTIAL_HEADER = re.compile(r"(?:X-API-Key|Authorization)\s*:", re.IGNORECASE)


def required_native_artifacts() -> tuple[str, ...]:
    """Return the reviewed native evidence inventory, including the AX-only menu."""
    return (
        "native-safari-result.json",
        "workflow3-editor-before-fix.png",
        "workflow3-editor-after-reload.png",
        "workflow3-v5-node-menu-ax.txt",
        "workflow3-v5-saved.png",
        "workflow3-v5-saved-ax.txt",
        "workflow3-v5-restored.png",
        "workflow3-v5-restored-ax.txt",
        "workflow3-v5-preflight.png",
        "workflow3-v5-preflight-result.png",
        "workflow3-v5-preflight-ax.txt",
        "workflow3-v5-published.png",
        "workflow3-v5-published-ax.txt",
        "workflow3-v5-deploy-configured.png",
        "workflow3-v5-deployment-created.png",
        "workflow3-v5-deployment-created-ax.txt",
        "workflow3-v5-awaiting-approval.png",
        "workflow3-v5-awaiting-approval-ax.txt",
        "workflow3-v5-run-failed-sla.png",
        "workflow3-v5-run-chain-verified.png",
        "workflow3-v5-chain-verified-ax.txt",
        "workflow3-v5-runs-painted-after-shell-fix.jpeg",
        "workflow3-v5-runs-painted-after-shell-fix-ax.txt",
        "workflow3-v5-chain-verified-painted-after-shell-fix.jpeg",
        "workflow3-v5-chain-verified-painted-after-shell-fix-ax.txt",
        "workflow3-stable-v4-restored.png",
        "workflow3-stable-v4-restored-ax.txt",
        "backend-restart-v5.txt",
        "backend-restore-v4.txt",
        "health-serving-v5.json",
        "health-restored-v4.json",
        "deployments-after-restore.json",
    )


def _nested(value: Mapping[str, Any], *fields: str) -> Any:
    current: Any = value
    for field in fields:
        if not isinstance(current, Mapping):
            return None
        current = current.get(field)
    return current


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise RuntimeError("required lifecycle timestamp is missing")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def validate_observations(proof: Mapping[str, Any]) -> None:
    """Fail closed unless every live and persistence invariant is exact."""
    workflow = proof.get("workflow")
    if not isinstance(workflow, Mapping) or (
        workflow.get("id"),
        workflow.get("status"),
        workflow.get("version"),
    ) != (WORKFLOW_ID, "published", 5):
        raise RuntimeError("workflow is not the exact published v5 graph")
    nodes = workflow.get("nodes")
    edges = workflow.get("edges")
    if not isinstance(nodes, list) or len(nodes) != 4:
        raise RuntimeError("published v5 does not contain exactly four nodes")
    if not isinstance(edges, list) or len(edges) != 4:
        raise RuntimeError("published v5 does not contain exactly four edges")

    diff = proof.get("diff")
    if not isinstance(diff, Mapping) or (
        diff.get("left_graph_id"),
        diff.get("left_version"),
        diff.get("right_graph_id"),
        diff.get("right_version"),
    ) != (WORKFLOW_ID, 4, WORKFLOW_ID, 5):
        raise RuntimeError("v4-to-v5 diff identity is not exact")
    if any(diff.get(field) != [] for field in _DIFF_CHANGE_FIELDS):
        raise RuntimeError("v4-to-v5 diff contains a change")

    deployments = (
        (
            "fresh",
            proof.get("fresh_deployment"),
            (FRESH_DEPLOYMENT, 1, FRESH_GRAPH, "active", False),
        ),
        (
            "stable",
            proof.get("stable_deployment"),
            (STABLE_DEPLOYMENT, 3, STABLE_GRAPH, "active", True),
        ),
    )
    for label, deployment, expected in deployments:
        if not isinstance(deployment, Mapping) or (
            deployment.get("deployment_ref"),
            deployment.get("version"),
            deployment.get("graph_version_ref"),
            deployment.get("status"),
            deployment.get("serving"),
        ) != expected:
            raise RuntimeError(f"{label} deployment lifecycle state is not exact")

    health = proof.get("health")
    if not isinstance(health, Mapping) or (
        health.get("status"),
        health.get("deployment_ref"),
        health.get("deployment_version"),
        health.get("graph_version_ref"),
        health.get("campaign_id"),
    ) != ("ok", STABLE_DEPLOYMENT, 3, STABLE_GRAPH, TENANT):
        raise RuntimeError("live health does not identify the restored stable graph")

    run = proof.get("run")
    run_identity = (RUN_ID, RUN_ID, FRESH_DEPLOYMENT, FRESH_GRAPH, TENANT, None)
    if not isinstance(run, Mapping) or (
        run.get("run_id"),
        run.get("thread_id"),
        run.get("deployment_ref"),
        run.get("graph_version_ref"),
        run.get("tenant_id"),
        run.get("workspace_id"),
    ) != run_identity:
        raise RuntimeError("run is not bound to the fresh v5 deployment")
    if run.get("status") != "failed" or _nested(run, "failure_state", "reason") != (
        "approval_rejected"
    ):
        raise RuntimeError("run did not fail closed from approval rejection")

    approval = proof.get("approval")
    if not isinstance(approval, Mapping) or (
        approval.get("approval_id"),
        approval.get("run_id"),
        approval.get("thread_id"),
        approval.get("deployment_ref"),
        approval.get("graph_version_ref"),
        approval.get("tenant_id"),
        approval.get("workspace_id"),
    ) != (APPROVAL_ID, *run_identity):
        raise RuntimeError("approval is not bound to the exact fresh v5 run")
    if approval.get("status") != "resolved" or _nested(
        approval, "urgency_metadata", "sla_timeout_seconds"
    ) != 5:
        raise RuntimeError("approval did not resolve under the five-second SLA")
    if (
        _nested(approval, "resolution", "decision"),
        _nested(approval, "resolution", "actor", "subject"),
        _nested(approval, "resolution", "actor", "tenant_id"),
        _nested(approval, "resolution", "actor", "workspace_id"),
    ) != ("reject", "sla_enforcer", TENANT, None):
        raise RuntimeError("approval was not rejected by the scoped SLA enforcer")
    created = _timestamp(approval.get("created_at"))
    deadline = _timestamp(approval.get("sla_deadline"))
    resolved = _timestamp(_nested(approval, "resolution", "resolved_at"))
    if (deadline - created).total_seconds() != 5 or not created < deadline <= resolved:
        raise RuntimeError("approval timestamps do not prove exact SLA expiry")

    audits = proof.get("audits")
    if not isinstance(audits, list) or len(audits) != 3:
        raise RuntimeError("expected exactly three signed audit records")
    for sequence, audit in enumerate(audits, start=1):
        if not isinstance(audit, Mapping) or (
            audit.get("run_id"),
            audit.get("thread_id"),
            audit.get("deployment_ref"),
            audit.get("graph_version_ref"),
            audit.get("tenant_id"),
            audit.get("workspace_id"),
            audit.get("chain_sequence"),
        ) != (*run_identity, sequence):
            raise RuntimeError("audit chain identity or sequence is incomplete")
        if not all(
            isinstance(audit.get(field), str) and bool(audit.get(field))
            for field in (
                "audit_id",
                "record_digest",
                "record_signature",
                "signing_key_id",
                "signing_algorithm",
            )
        ):
            raise RuntimeError("audit record lacks signed provenance")

    verification = proof.get("verification")
    if not isinstance(verification, Mapping) or (
        verification.get("verified"),
        verification.get("signature_verified"),
        verification.get("record_count"),
        verification.get("unsigned_record_count"),
    ) != (True, True, 3, 0):
        raise RuntimeError("three-record signed chain did not verify")

    for field in (
        "side_effect_operation_rows",
        "execution_event_rows",
        "reservation_rows",
        "action_markers_created_since_run",
    ):
        if proof.get(field) != 0:
            raise RuntimeError(f"negative lifecycle produced non-zero {field}")

    readiness = proof.get("audit_readiness")
    if not isinstance(readiness, Mapping) or (
        readiness.get("ready"),
        readiness.get("state"),
        readiness.get("signer_available"),
    ) != (True, "signed", True):
        raise RuntimeError("audit readiness is not signed")


def find_secret_leaks(root: Path, credential: str) -> list[str]:
    """Return only leaking relative filenames; never return credential content."""
    if not credential:
        raise RuntimeError("persistent service credential is missing")
    needle = credential.encode()
    return [
        path.relative_to(root).as_posix()
        for path in sorted(root.rglob("*"))
        if path.is_file() and needle in path.read_bytes()
    ]


def sanitize_ax_text(value: str) -> str:
    """Replace each full credential-header line while preserving AX semantics."""
    sanitized = [
        "[credential header omitted]" if _CREDENTIAL_HEADER.search(line) else line
        for line in value.splitlines()
    ]
    return "\n".join(sanitized) + ("\n" if value.endswith("\n") else "")


class _Api:
    def __init__(self, credential: str) -> None:
        self._client = httpx.Client(
            base_url=BASE_URL,
            headers={"X-API-Key": credential, "X-Tenant-ID": TENANT},
            timeout=15,
        )

    def get(self, path: str) -> Any:
        response = self._client.get(path)
        if response.status_code != 200:
            raise RuntimeError(f"GET {path} returned {response.status_code}")
        return response.json()

    def post(self, path: str) -> Any:
        response = self._client.post(path)
        if response.status_code != 200:
            raise RuntimeError(f"POST {path} returned {response.status_code}")
        return response.json()

    def close(self) -> None:
        self._client.close()


def _read_rows(path: Path, query: str, parameters: tuple[object, ...] = ()) -> list[dict[str, Any]]:
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as database:
        database.row_factory = sqlite3.Row
        return [dict(row) for row in database.execute(query, parameters).fetchall()]


def _collect_persistence() -> tuple[dict[str, Any], datetime]:
    run_rows = _read_rows(
        MAIN_DB,
        """
        SELECT run_id, started_at FROM runs
        WHERE tenant_id = ? AND workspace_scope = 'null' AND run_id = ?
        """,
        (TENANT, RUN_ID),
    )
    if len(run_rows) != 1:
        raise RuntimeError("exact run persistence record is missing")
    started_at = _timestamp(run_rows[0]["started_at"])
    operation_rows = _read_rows(
        MAIN_DB,
        """
        SELECT operation_key FROM side_effect_operations
        WHERE tenant_id = ? AND workspace_scope = 'null' AND run_id = ?
        """,
        (TENANT, RUN_ID),
    )
    marker_rows = _read_rows(SINK_DB, "SELECT created_at FROM action_markers")
    marker_count = sum(
        _timestamp(row["created_at"]) >= started_at
        for row in marker_rows
        if isinstance(row.get("created_at"), str)
    )
    return (
        {
            "run_id": RUN_ID,
            "run_started_at": started_at.isoformat(),
            "side_effect_operation_rows": len(operation_rows),
            "action_markers_created_since_run": marker_count,
        },
        started_at,
    )


def _collect_economics() -> dict[str, Any]:
    events = _read_rows(
        ECON_DB,
        """
        SELECT execution_id FROM execution_events
        WHERE tenant_id = ? AND (execution_id = ? OR join_key = ?)
        """,
        (TENANT, RUN_ID, RUN_ID),
    )
    reservations = _read_rows(
        ECON_DB,
        "SELECT operation_id FROM cost_reservations WHERE tenant_id = ? AND run_id = ?",
        (TENANT, RUN_ID),
    )
    return {
        "run_id": RUN_ID,
        "execution_event_rows": len(events),
        "reservation_rows": len(reservations),
        "disposition": "provider_free_zero_activity",
    }


def _safe_audits(audits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fields = (
        "audit_id",
        "run_id",
        "thread_id",
        "node_id",
        "deployment_ref",
        "graph_version_ref",
        "tenant_id",
        "workspace_id",
        "status",
        "actor",
        "chain_sequence",
        "previous_record_digest",
        "record_digest",
        "record_signature",
        "signing_key_id",
        "signing_algorithm",
        "started_at",
        "completed_at",
    )
    return [{field: audit.get(field) for field in fields} for audit in audits]


def _safe_run(run: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "run_id",
        "thread_id",
        "status",
        "current_step",
        "deployment_ref",
        "graph_version_ref",
        "tenant_id",
        "workspace_id",
        "campaign_id",
        "failure_state",
        "traversal",
    )
    return {field: run.get(field) for field in fields}


def _safe_approval(approval: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "approval_id",
        "run_id",
        "thread_id",
        "node_id",
        "deployment_ref",
        "graph_version_ref",
        "tenant_id",
        "workspace_id",
        "status",
        "urgency_metadata",
        "resolution",
        "sla_deadline",
        "escalation_action",
        "created_at",
        "updated_at",
    )
    return {field: approval.get(field) for field in fields}


def _collect_live(credential: str) -> tuple[dict[str, Any], dict[str, Any]]:
    api = _Api(credential)
    try:
        workflow = api.get(f"/api/studio/v1/workflows/{WORKFLOW_ID}")
        diff = api.get(f"/api/studio/v1/workflows/{WORKFLOW_ID}/diff?left=4&right=5")
        deployments = api.get("/v1/deployments")
        health = api.get("/health")
        run = api.get(f"/v1/runs/{RUN_ID}")
        evidence = api.get(f"/v1/runs/{RUN_ID}/evidence")
        verification = api.post(f"/v1/runs/{RUN_ID}/verify-chain")
        readiness = api.get("/v1/audit-readiness")
    finally:
        api.close()
    if not isinstance(deployments, list) or not isinstance(evidence, Mapping):
        raise RuntimeError("live deployment or run evidence response is malformed")
    fresh = next(
        (
            item
            for item in deployments
            if isinstance(item, dict)
            and item.get("deployment_ref") == FRESH_DEPLOYMENT
            and item.get("version") == 1
        ),
        None,
    )
    stable = next(
        (
            item
            for item in deployments
            if isinstance(item, dict)
            and item.get("deployment_ref") == STABLE_DEPLOYMENT
            and item.get("version") == 3
        ),
        None,
    )
    approvals = evidence.get("approvals")
    audit_values = evidence.get("audits")
    if not isinstance(approvals, list) or not isinstance(audit_values, list):
        raise RuntimeError("run evidence lacks approvals or audits")
    approval = next(
        (
            item
            for item in approvals
            if isinstance(item, dict) and item.get("approval_id") == APPROVAL_ID
        ),
        None,
    )
    audits = [item for item in audit_values if isinstance(item, dict)]
    if not all(
        isinstance(value, dict)
        for value in (workflow, diff, fresh, stable, health, run, approval, verification, readiness)
    ):
        raise RuntimeError("one or more exact live lifecycle records are missing")
    persistence, _started_at = _collect_persistence()
    economics = _collect_economics()
    proof = {
        "workflow": workflow,
        "diff": diff,
        "fresh_deployment": fresh,
        "stable_deployment": stable,
        "health": health,
        "run": run,
        "approval": approval,
        "audits": audits,
        "verification": verification,
        "side_effect_operation_rows": persistence["side_effect_operation_rows"],
        "action_markers_created_since_run": persistence["action_markers_created_since_run"],
        "execution_event_rows": economics["execution_event_rows"],
        "reservation_rows": economics["reservation_rows"],
        "audit_readiness": readiness,
    }
    validate_observations(proof)
    observations = {
        "workflow": workflow,
        "diff": diff,
        "deployments": {"fresh": fresh, "stable": stable},
        "health": health,
        "run": _safe_run(run),
        "approval": _safe_approval(approval),
        "audits": _safe_audits(audits),
        "verification": verification,
        "persistence": persistence,
        "economics": economics,
        "audit_readiness": readiness,
    }
    return proof, observations


def _validate_native_source(credential: str) -> None:
    missing = [name for name in required_native_artifacts() if not (NATIVE_ROOT / name).is_file()]
    if missing:
        raise RuntimeError(f"native evidence is incomplete; missing {len(missing)} artifact(s)")
    menu = (NATIVE_ROOT / "workflow3-v5-node-menu-ax.txt").read_text(encoding="utf-8")
    expected = (
        "Entrypoint",
        "Agent",
        "Code",
        "Executable Unit",
        "Human Approval",
        "Loop",
        "Retrieval",
        "Subgraph",
    )
    if "menu Node types" not in menu or any(label not in menu for label in expected):
        raise RuntimeError("native node-menu AX evidence does not expose all eight options")
    image_names = [
        name for name in required_native_artifacts() if name.endswith((".png", ".jpeg", ".jpg"))
    ]
    for name in image_names:
        payload = (NATIVE_ROOT / name).read_bytes()
        if credential.encode() in payload or _CREDENTIAL_HEADER.search(
            payload.decode("utf-8", errors="ignore")
        ):
            raise RuntimeError(f"native screenshot binary scan failed: {name}")


def _run_check(argv: list[str]) -> tuple[int, str, str]:
    result = subprocess.run(
        argv,
        cwd=WORKTREE,
        capture_output=True,
        text=True,
        timeout=300,
    )
    return result.returncode, result.stdout, result.stderr


def _artifact_destination(name: str) -> str:
    if name.endswith(".png"):
        return f"screenshots/{Path(name).with_suffix('.jpeg').name}"
    if name.endswith((".jpeg", ".jpg")):
        return f"screenshots/{name}"
    if name.endswith("-ax.txt"):
        return f"accessibility/{name}"
    return f"console/native-{name}"


def _ingest_native(store: EvidenceStore, name: str, destination: str) -> None:
    source = NATIVE_ROOT / name
    if name.endswith("-ax.txt"):
        projected = sanitize_ax_text(source.read_text(encoding="utf-8"))
        store.validate(projected)
        store._atomic_bytes_exclusive(Path(destination), projected.encode())
        return
    store.ingest_artifact(source, destination)


def _write_json(store: EvidenceStore, name: str, value: object) -> str:
    relative = Path("console") / f"{name}.json"
    store._write_exclusive(relative, value)
    return relative.as_posix()


def _source_sha256() -> dict[str, str]:
    paths = (
        WORKTREE / "release/live_evaluation/workflow3_v5_lifecycle_checkpoint.py",
        WORKTREE / "tests/live_evaluation/test_workflow3_v5_lifecycle_checkpoint.py",
    )
    return {
        path.relative_to(WORKTREE).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths
    }


def _criteria(paths: Mapping[str, str], artifacts: Mapping[str, str]) -> list[AcceptanceCriterion]:
    ui = tuple(
        artifacts[name]
        for name in required_native_artifacts()
        if name.endswith((".png", "-ax.txt"))
    )
    return [
        AcceptanceCriterion(
            ACCEPTED_CRITERIA[0],
            "pass",
            ui
            + (
                paths["workflow"],
                paths["run"],
                paths["verification"],
            ),
        ),
        AcceptanceCriterion(
            ACCEPTED_CRITERIA[1],
            "pass",
            (
                artifacts["workflow3-v5-published.png"],
                artifacts["workflow3-v5-deployment-created.png"],
                artifacts["backend-restart-v5.txt"],
                artifacts["health-serving-v5.json"],
                artifacts["backend-restore-v4.txt"],
                artifacts["deployments-after-restore.json"],
                paths["deployments"],
            ),
        ),
        AcceptanceCriterion(
            ACCEPTED_CRITERIA[2],
            "pass",
            (
                artifacts["workflow3-v5-awaiting-approval.png"],
                artifacts["workflow3-v5-run-failed-sla.png"],
                artifacts["workflow3-v5-run-chain-verified.png"],
                paths["run"],
                paths["approval"],
                paths["audits"],
                paths["verification"],
                paths["persistence"],
                paths["economics"],
                paths["readiness"],
            ),
        ),
        AcceptanceCriterion(
            ACCEPTED_CRITERIA[3],
            "pass",
            (
                artifacts["health-restored-v4.json"],
                artifacts["backend-restore-v4.txt"],
                paths["health"],
            ),
        ),
    ]


def build() -> None:
    """Re-query, validate, ingest, scan, checksum, and seal exactly once."""
    if ROOT.exists():
        raise RuntimeError(f"evidence root is not fresh: {ROOT}")
    credential = SERVICE_KEY_PATH.read_text(encoding="utf-8").strip()
    if not credential:
        raise RuntimeError("persistent service credential is missing")
    _validate_native_source(credential)
    _proof, observations = _collect_live(credential)

    checks = (
        (
            1,
            "focused-pytest",
            [
                "uv",
                "run",
                "pytest",
                "-q",
                "tests/live_evaluation/test_workflow3_v5_lifecycle_checkpoint.py",
            ],
        ),
        (
            2,
            "focused-ruff",
            [
                "uv",
                "run",
                "ruff",
                "check",
                "release/live_evaluation/workflow3_v5_lifecycle_checkpoint.py",
                "tests/live_evaluation/test_workflow3_v5_lifecycle_checkpoint.py",
            ],
        ),
    )
    completed_checks: list[tuple[int, str, list[str], int, str, str]] = []
    for sequence, name, argv in checks:
        exit_code, stdout, stderr = _run_check(argv)
        if exit_code:
            raise RuntimeError(f"{name} failed with exit code {exit_code}")
        completed_checks.append((sequence, name, argv, exit_code, stdout, stderr))

    store = EvidenceStore(ROOT)
    for sequence, name, argv, exit_code, stdout, stderr in completed_checks:
        store.record_command(
            sequence=sequence,
            name=name,
            argv=argv,
            working_directory=WORKTREE,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
        )

    paths = {
        "workflow": _write_json(
            store,
            "workflow-published-v5",
            {
                "id": observations["workflow"]["id"],
                "status": observations["workflow"]["status"],
                "version": observations["workflow"]["version"],
                "nodes": observations["workflow"]["nodes"],
                "edges": observations["workflow"]["edges"],
            },
        ),
        "diff": _write_json(store, "workflow-diff-v4-v5", observations["diff"]),
        "deployments": _write_json(store, "deployments-restored", observations["deployments"]),
        "health": _write_json(store, "health-restored-stable", observations["health"]),
        "run": _write_json(store, "run-failed-approval-rejected", observations["run"]),
        "approval": _write_json(store, "approval-sla-rejected", observations["approval"]),
        "audits": _write_json(store, "three-signed-audits", observations["audits"]),
        "verification": _write_json(
            store, "signed-chain-verification", observations["verification"]
        ),
        "persistence": _write_json(
            store, "zero-side-effects-and-markers", observations["persistence"]
        ),
        "economics": _write_json(store, "zero-economics-activity", observations["economics"]),
        "readiness": _write_json(store, "signed-audit-readiness", observations["audit_readiness"]),
    }

    artifacts: dict[str, str] = {}
    for name in required_native_artifacts():
        destination = _artifact_destination(name)
        _ingest_native(store, name, destination)
        artifacts[name] = destination

    store.append_event(
        "workflow.lifecycle.observed",
        {
            "workflow_evidence": paths["workflow"],
            "diff_evidence": paths["diff"],
            "deployment_evidence": paths["deployments"],
            "result": "pass",
        },
        correlation=CorrelationIds(run_id=RUN_ID, ui_action_id="native-safari-workflow3-v5"),
    )
    store.append_event(
        "run.sla_expiry.observed",
        {
            "run_evidence": paths["run"],
            "approval_evidence": paths["approval"],
            "persistence_evidence": paths["persistence"],
            "economics_evidence": paths["economics"],
            "result": "pass",
        },
        correlation=CorrelationIds(run_id=RUN_ID),
    )
    store.append_event(
        "audit.chain.verified",
        {
            "audit_evidence": paths["audits"],
            "verification_evidence": paths["verification"],
            "readiness_evidence": paths["readiness"],
            "result": "pass",
        },
        correlation=CorrelationIds(
            run_id=RUN_ID,
            audit_event_id=f"chain-verification:{RUN_ID}",
        ),
    )

    criteria = _criteria(paths, artifacts)
    store.write_manifest(
        {
            "schema_version": 1,
            "campaign_id": TENANT,
            "checkpoint": "workflow3-v5-native-safari-lifecycle",
            "created_at": datetime.now(UTC).isoformat(),
            "revision": subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=WORKTREE,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
            "source_sha256": _source_sha256(),
            "native_source_root": NATIVE_ROOT.name,
            "runtime": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "backend_url": BASE_URL,
                "browser": "Safari operated through macOS Computer Use",
            },
            "configuration": {
                "workflow_id": WORKFLOW_ID,
                "workflow_version": 5,
                "fresh_deployment_ref": FRESH_DEPLOYMENT,
                "fresh_deployment_version": 1,
                "fresh_graph_version_ref": FRESH_GRAPH,
                "stable_deployment_ref": STABLE_DEPLOYMENT,
                "stable_deployment_version": 3,
                "stable_graph_version_ref": STABLE_GRAPH,
                "run_id": RUN_ID,
                "approval_id": APPROVAL_ID,
            },
            "accepted_criteria": list(ACCEPTED_CRITERIA),
            "database_sha256": {
                "zeroth": hashlib.sha256(MAIN_DB.read_bytes()).hexdigest(),
                "economics": hashlib.sha256(ECON_DB.read_bytes()).hexdigest(),
                "action_sink": hashlib.sha256(SINK_DB.read_bytes()).hexdigest(),
            },
        }
    )
    store._write_exclusive(
        Path("results.json"),
        {
            "schema_version": 1,
            "completed": True,
            "criteria": [
                {
                    "criterion_id": criterion.criterion_id,
                    "status": criterion.status,
                    "evidence": list(criterion.evidence),
                }
                for criterion in criteria
            ],
            "native_artifacts": artifacts,
        },
    )
    leaks = find_secret_leaks(ROOT, credential)
    if leaks:
        raise RuntimeError(f"credential scan failed in {len(leaks)} evidence file(s)")
    _write_json(
        store,
        "credential-scan",
        {"files_scanned": sum(path.is_file() for path in ROOT.rglob("*")), "leak_count": 0},
    )
    criteria = _criteria(paths, artifacts)
    store.finalize_bundle(
        acceptance=criteria,
        report_markdown=(
            "# Workflow 3 v5 lifecycle checkpoint\n\n"
            "Native Safari evidence and fresh live queries jointly prove the complete Workflow 3 "
            "v5 publish, deployment, restart, governed-run, SLA rejection, signed-chain, and "
            "stable restore lifecycle. Published v5 has four nodes and four edges, and its "
            "structured diff "
            "from v4 contains no changes. The fresh v5 deployment remains active and non-serving "
            "after the stable v4 deployment was restored to serving health.\n\n"
            "Run `3959264c97974cb39c3e43035afac293` failed closed with "
            "`approval_rejected`. Approval `aa48535adc09473583305aa281c346f5` was rejected by "
            "`sla_enforcer` after its five-second deadline. All three audit records are signed, "
            "the "
            "chain verifies, and signed audit readiness is available. The run has zero side-effect "
            "operations, zero action markers since start, zero economics execution events, and "
            "zero "
            "cost reservations.\n\n"
            "Exactly the four criteria in `acceptance.json` pass. The node-menu checkpoint is the "
            "complete eight-option native accessibility capture; no node-menu screenshot existed, "
            "so none is claimed. All ingested artifacts and observations passed recursive "
            "credential "
            "scanning and are sealed by `SHA256SUMS`.\n"
            "\nThe initial one-shot attempt is preserved outside this sealed bundle at "
            "`workflow3-v5-lifecycle-checkpoint-20260824-1-failed-mime-diagnostic`; it stopped "
            "before finalization when the native `.png` names were found to contain JPEG bytes.\n"
        ),
    )


if __name__ == "__main__":
    build()
