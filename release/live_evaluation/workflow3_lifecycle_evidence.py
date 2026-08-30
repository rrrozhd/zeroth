"""Build a directly reproducible Workflow 3 lifecycle evidence bundle.

This collector deliberately executes every recorded command. It preserves only
sanitized, acceptance-relevant fields from live responses and binds both tracked
diffs and untracked file bytes into the working-tree digest.
"""

# Evidence prose and immutable checkpoint vectors are intentionally verbatim.
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import os
import platform
import sqlite3
import subprocess
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .evidence import AcceptanceCriterion, CorrelationIds, EvidenceStore
from .runtime_paths import resolve_runtime_paths

_RUNTIME_PATHS = resolve_runtime_paths()
WORKTREE = _RUNTIME_PATHS.worktree
STATE_ROOT = _RUNTIME_PATHS.state_root
ROOT = STATE_ROOT / "evidence/workflow3-lifecycle-20260824-1"
SAFARI_ROOT = STATE_ROOT / "evidence/workflow3-lifecycle-20260824-1-staging"
PLAYWRIGHT_ROOT = STATE_ROOT / "evidence/workflow3-lifecycle-ui-20260824-1"
SERVICE_KEY_PATH = STATE_ROOT / "runtime-secrets/service-api-key"
RUN_ID = "314e7c166f6840ba978af8e8045b78c0"
WORKFLOW_ID = "evaluation-studio-v1-governed-remediation"
DEPLOYMENT = "evaluation-studio-v1-governed-remediation-v2"
DEPLOYMENT_VERSION = 2
GRAPH = "evaluation-studio-v1-governed-remediation@3"
APPROVAL_ID = "0fcb5f54c82f4115942b8b9828673277"


def _git(repo: Path, *args: str, binary: bool = False) -> bytes | str:
    result = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=not binary
    )
    return result.stdout if not binary else result.stdout


def _tree_digest(repo: Path = WORKTREE) -> str:
    """Hash tracked changes and the bytes of every untracked regular file."""
    digest = hashlib.sha256()
    digest.update(str(_git(repo, "status", "--porcelain=v1", "-z")).encode())
    tracked_diff = _git(repo, "diff", "--binary", "HEAD", binary=True)
    assert isinstance(tracked_diff, bytes)
    digest.update(tracked_diff)
    untracked = _git(repo, "ls-files", "--others", "--exclude-standard", "-z", binary=True)
    assert isinstance(untracked, bytes)
    for raw_name in sorted(value for value in untracked.split(b"\0") if value):
        relative = Path(os.fsdecode(raw_name))
        source = repo / relative
        if not source.is_file() or source.is_symlink():
            continue
        digest.update(b"untracked\0")
        digest.update(raw_name)
        digest.update(b"\0")
        digest.update(hashlib.sha256(source.read_bytes()).digest())
    return digest.hexdigest()


def _source_hashes(paths: list[Path]) -> dict[str, str]:
    return {
        path.relative_to(WORKTREE).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths
    }


def _request(path: str, *, method: str = "GET") -> dict[str, Any] | list[Any]:
    credential = SERVICE_KEY_PATH.read_text().strip()
    request = urllib.request.Request(
        f"http://127.0.0.1:8122{path}",
        method=method,
        headers={"X-API-Key": credential},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        value = json.load(response)
    if not isinstance(value, (dict, list)):
        raise RuntimeError(f"expected structured response from {path}")
    return value


def _run_recorded(
    store: EvidenceStore,
    *,
    sequence: int,
    name: str,
    argv: list[str],
    cwd: Path,
    env: dict[str, str] | None = None,
) -> None:
    result = subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )
    store.record_command(
        sequence=sequence,
        name=name,
        argv=argv,
        working_directory=cwd,
        exit_code=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )
    if result.returncode:
        raise RuntimeError(f"recorded command failed: {name} ({result.returncode})")


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise RuntimeError("missing acceptance timestamp")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _validate_observations(
    *,
    workflow: dict[str, Any],
    deployment: dict[str, Any],
    health: dict[str, Any],
    run: dict[str, Any],
    approval: dict[str, Any],
    audits: list[dict[str, Any]],
    verification: dict[str, Any],
    side_effect_count: int,
    marker_count_since_run: int,
    expected_workflow_version: int = 3,
    expected_deployment_version: int = DEPLOYMENT_VERSION,
    expected_graph: str = GRAPH,
    expected_run_id: str = RUN_ID,
    expected_approval_id: str = APPROVAL_ID,
) -> None:
    if (workflow.get("id"), workflow.get("status"), workflow.get("version")) != (
        WORKFLOW_ID,
        "published",
        expected_workflow_version,
    ):
        raise RuntimeError("workflow is not the published v3 UI lifecycle graph")
    expected_deployment = (
        DEPLOYMENT,
        expected_deployment_version,
        expected_graph,
        "active",
        True,
    )
    observed_deployment = (
        deployment.get("deployment_ref"),
        deployment.get("version"),
        deployment.get("graph_version_ref"),
        deployment.get("status"),
        deployment.get("serving"),
    )
    if observed_deployment != expected_deployment:
        raise RuntimeError("deployment does not identify the active Workflow 3 graph")
    expected_health = ("ok", DEPLOYMENT, expected_deployment_version, expected_graph)
    observed_health = (
        health.get("status"),
        health.get("deployment_ref"),
        health.get("deployment_version"),
        health.get("graph_version_ref"),
    )
    if observed_health != expected_health:
        raise RuntimeError("post-restart health does not identify the exact graph")
    identity = (
        expected_run_id,
        expected_run_id,
        DEPLOYMENT,
        expected_graph,
    )
    if (
        run.get("run_id"),
        run.get("thread_id"),
        run.get("deployment_ref"),
        run.get("graph_version_ref"),
    ) != identity or run.get("status") != "failed":
        raise RuntimeError("run identity or terminal state is incorrect")
    failure_state = run.get("failure_state")
    if not isinstance(failure_state, dict) or failure_state.get("reason") != "approval_rejected":
        raise RuntimeError("run did not fail from approval rejection")
    if (
        approval.get("approval_id"),
        approval.get("run_id"),
        approval.get("thread_id"),
        approval.get("deployment_ref"),
        approval.get("graph_version_ref"),
    ) != (expected_approval_id, *identity):
        raise RuntimeError("approval is not bound to the exact run and deployment")
    urgency = approval.get("urgency_metadata")
    resolution = approval.get("resolution")
    if not isinstance(urgency, dict) or urgency.get("sla_timeout_seconds") != 5:
        raise RuntimeError("approval SLA is not the configured five seconds")
    if not isinstance(resolution, dict):
        raise RuntimeError("approval resolution is missing")
    actor = resolution.get("actor")
    if (
        resolution.get("decision") != "reject"
        or not isinstance(actor, dict)
        or actor.get("subject") != "sla_enforcer"
    ):
        raise RuntimeError("approval was not rejected by the SLA enforcer")
    run_scope = (run.get("tenant_id"), run.get("workspace_id"))
    approval_scope = (approval.get("tenant_id"), approval.get("workspace_id"))
    actor_scope = (actor.get("tenant_id"), actor.get("workspace_id"))
    if approval_scope != run_scope or actor_scope != approval_scope:
        raise RuntimeError("approval actor tenant or workspace correlation is incomplete")
    created = _parse_timestamp(approval.get("created_at"))
    deadline = _parse_timestamp(approval.get("sla_deadline"))
    resolved = _parse_timestamp(resolution.get("resolved_at"))
    if not created < deadline <= resolved:
        raise RuntimeError("approval resolution did not occur on or after the SLA deadline")
    if len(audits) != 3:
        raise RuntimeError("expected the three directly observed audit records")
    for sequence, audit in enumerate(audits, start=1):
        if (
            audit.get("run_id"),
            audit.get("thread_id"),
            audit.get("deployment_ref"),
            audit.get("graph_version_ref"),
        ) != identity:
            raise RuntimeError("audit identity correlation is incomplete")
        if audit.get("chain_sequence") != sequence:
            raise RuntimeError("audit chain sequence is incomplete")
        audit_scope = (audit.get("tenant_id"), audit.get("workspace_id"))
        if audit_scope != run_scope:
            raise RuntimeError("audit tenant or workspace correlation is incomplete")
        if not all(
            isinstance(audit.get(field), str) and audit.get(field)
            for field in ("audit_id", "record_digest", "record_signature", "signing_key_id", "signing_algorithm")
        ):
            raise RuntimeError("audit signature evidence is incomplete")
    if not (
        verification.get("verified") is True
        and verification.get("signature_verified") is True
        and verification.get("record_count") == 3
        and verification.get("unsigned_record_count") == 0
    ):
        raise RuntimeError("signed audit chain did not verify")
    if side_effect_count != 0 or marker_count_since_run != 0:
        raise RuntimeError("SLA-expired approval produced a side effect")


def _write_observation(store: EvidenceStore, name: str, value: object) -> str:
    relative = Path("console") / f"{name}.json"
    store._write_exclusive(relative, value)
    return relative.as_posix()


def _acceptance_criteria(
    paths: dict[str, str], *, lifecycle_event: str | None = None
) -> list[AcceptanceCriterion]:
    """Apply the independent screenshot-first disposition to this slice."""
    health_evidence = (paths["health"],)
    if lifecycle_event is not None:
        health_evidence += (f"events.ndjson#{lifecycle_event}",)
    missing_ui_note = (
        "Native Safari checkpoint screenshots and timestamped Computer Use actions are required."
    )
    provisional_backend_note = (
        "Behavioral evidence exists, but campaign acceptance still requires native Safari checkpoint "
        "screenshots, tenant/authorization joins, economics evidence, and durable query results."
    )
    return [
        AcceptanceCriterion("ui.node-menu", "not_run", note=missing_ui_note),
        AcceptanceCriterion("ui.keyboard-shortcuts", "not_run", note=missing_ui_note),
        AcceptanceCriterion("ui.publish-deploy-run", "not_run", note=missing_ui_note),
        AcceptanceCriterion(
            "workflow3.health-exact-graph-version", "pass", health_evidence
        ),
        AcceptanceCriterion(
            "workflow3.publish-deploy-restart", "not_run", note=provisional_backend_note
        ),
        AcceptanceCriterion(
            "workflow3.negative-sla-expiry", "not_run", note=provisional_backend_note
        ),
    ]


def _poll_health() -> dict[str, Any]:
    last_error: Exception | None = None
    for _ in range(60):
        try:
            value = _request("/health")
            if isinstance(value, dict) and value.get("status") == "ok":
                return value
        except Exception as exc:  # service may close connections during restart
            last_error = exc
        time.sleep(0.5)
    raise RuntimeError("backend did not become healthy after restart") from last_error


def _select_artifact(glob_pattern: str) -> Path:
    matches = sorted(PLAYWRIGHT_ROOT.glob(glob_pattern))
    if not matches:
        raise RuntimeError(f"missing rendered evidence artifact: {glob_pattern}")
    return matches[0]


def build() -> None:
    store = EvidenceStore(ROOT)
    if store.is_sealed or any(ROOT.iterdir()):
        raise RuntimeError(f"evidence root is not fresh: {ROOT}")

    _run_recorded(
        store,
        sequence=1,
        name="frontend-contract-tests",
        argv=[
            "npm",
            "test",
            "--",
            "--run",
            "app/studio/edit/runPayload.test.ts",
            "app/runs/page.test.tsx",
            "app/studio/edit/page.test.tsx",
        ],
        cwd=WORKTREE / "frontend",
    )
    _run_recorded(
        store,
        sequence=2,
        name="frontend-production-build",
        argv=["npm", "run", "build"],
        cwd=WORKTREE / "frontend",
    )
    restart_environment = dict(os.environ)
    restart_environment["ZEROTH_DEV_DEPLOYMENT_REF"] = DEPLOYMENT
    _run_recorded(
        store,
        sequence=3,
        name="backend-docker-restart",
        argv=[
            "docker",
            "compose",
            "-f",
            "compose.dev.yml",
            "up",
            "-d",
            "--force-recreate",
            "backend",
        ],
        cwd=WORKTREE,
        env=restart_environment,
    )

    health_value = _poll_health()
    workflow_value = _request(f"/api/studio/v1/workflows/{WORKFLOW_ID}")
    deployments_value = _request("/v1/deployments")
    run_value = _request(f"/v1/runs/{RUN_ID}")
    evidence_value = _request(f"/v1/runs/{RUN_ID}/evidence")
    verification_value = _request(f"/v1/runs/{RUN_ID}/verify-chain", method="POST")
    if not all(
        isinstance(value, dict)
        for value in (health_value, workflow_value, run_value, evidence_value, verification_value)
    ) or not isinstance(deployments_value, list):
        raise RuntimeError("live endpoints did not return the expected response shapes")
    health = health_value
    workflow = workflow_value
    run = run_value
    evidence = evidence_value
    verification = verification_value
    deployments = [
        value
        for value in deployments_value
        if isinstance(value, dict) and value.get("deployment_ref") == DEPLOYMENT
    ]
    active = next(
        (
            value
            for value in deployments
            if value.get("version") == DEPLOYMENT_VERSION and value.get("status") == "active"
        ),
        None,
    )
    approvals = evidence.get("approvals")
    audits_value = evidence.get("audits")
    if not isinstance(active, dict) or not isinstance(approvals, list) or not isinstance(audits_value, list):
        raise RuntimeError("deployment, approval, or audit evidence is missing")
    approval = next(
        (value for value in approvals if isinstance(value, dict) and value.get("approval_id") == APPROVAL_ID),
        None,
    )
    audits = [value for value in audits_value if isinstance(value, dict)]
    if not isinstance(approval, dict):
        raise RuntimeError("target approval evidence is missing")

    with sqlite3.connect(STATE_ROOT / "zeroth.db") as database:
        side_effect_count = int(
            database.execute(
                "SELECT COUNT(*) FROM side_effect_operations WHERE run_id = ?", (RUN_ID,)
            ).fetchone()[0]
        )
    created = _parse_timestamp(approval.get("created_at"))
    with sqlite3.connect(STATE_ROOT / "action-sink/actions.sqlite3") as sink:
        created_values = [
            row[0] for row in sink.execute("SELECT created_at FROM action_markers").fetchall()
        ]
    marker_count_since_run = sum(
        _parse_timestamp(value) >= created for value in created_values if isinstance(value, str)
    )

    _validate_observations(
        workflow=workflow,
        deployment=active,
        health=health,
        run=run,
        approval=approval,
        audits=audits,
        verification=verification,
        side_effect_count=side_effect_count,
        marker_count_since_run=marker_count_since_run,
    )

    workflow_observation = {
        "id": workflow.get("id"),
        "name": workflow.get("name"),
        "status": workflow.get("status"),
        "version": workflow.get("version"),
        "entry_step": workflow.get("entry_step"),
        "node_ids": [value.get("id") for value in workflow.get("nodes", []) if isinstance(value, dict)],
        "edge_ids": [value.get("id") for value in workflow.get("edges", []) if isinstance(value, dict)],
    }
    run_observation = {
        field: run.get(field)
        for field in (
            "run_id",
            "thread_id",
            "status",
            "current_step",
            "deployment_ref",
            "graph_version_ref",
            "tenant_id",
            "campaign_id",
            "failure_state",
            "traversal",
        )
    }
    approval_observation = {
        field: approval.get(field)
        for field in (
            "approval_id",
            "run_id",
            "thread_id",
            "node_id",
            "graph_version_ref",
            "deployment_ref",
            "tenant_id",
            "status",
            "urgency_metadata",
            "resolution",
            "sla_deadline",
            "escalation_action",
            "created_at",
            "updated_at",
        )
    }
    audit_observation = [
        {
            field: audit.get(field)
            for field in (
                "audit_id",
                "run_id",
                "thread_id",
                "node_id",
                "graph_version_ref",
                "deployment_ref",
                "tenant_id",
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
        }
        for audit in audits
    ]
    persistence_observation = {
        "run_id": RUN_ID,
        "side_effect_operation_rows": side_effect_count,
        "action_markers_created_since_run": marker_count_since_run,
        "invariant": "approval expired before any side-effect operation was created",
    }

    paths = {
        "health": _write_observation(store, "health", health),
        "workflow": _write_observation(store, "workflow-published-v3", workflow_observation),
        "deployments": _write_observation(store, "deployment-history", deployments),
        "run": _write_observation(store, "run-terminal", run_observation),
        "approval": _write_observation(store, "approval-sla-expiry", approval_observation),
        "audits": _write_observation(store, "signed-audit-identities", audit_observation),
        "verification": _write_observation(store, "chain-verification", verification),
        "persistence": _write_observation(store, "side-effect-absence", persistence_observation),
    }

    safari_path = store.ingest_artifact(
        SAFARI_ROOT / "safari-lifecycle-ax.json",
        "accessibility/safari-lifecycle-ax.json",
    ).relative_to(ROOT).as_posix()
    screenshot_path = store.ingest_artifact(
        _select_artifact("artifacts/**/workflow-3-configured.png"),
        "screenshots/workflow3-v3-configured.png",
    ).relative_to(ROOT).as_posix()
    store.ingest_artifact(
        PLAYWRIGHT_ROOT / "results.json",
        "playwright-report/configured-results.json",
    ).relative_to(ROOT).as_posix()
    axe_path = store.ingest_artifact(
        _select_artifact("indexed/*-axe-wcag22-aa.json"),
        "accessibility/workflow3-axe-wcag22-aa.json",
    ).relative_to(ROOT).as_posix()
    network_path = store.ingest_artifact(
        _select_artifact("indexed/*-sanitized-network.json"),
        "network/workflow3-sanitized-network.json",
    ).relative_to(ROOT).as_posix()
    identity_path = store.ingest_artifact(
        _select_artifact("indexed/*-response-identities.json"),
        "network/workflow3-response-identities.json",
    ).relative_to(ROOT).as_posix()
    video_path = store.ingest_artifact(
        _select_artifact("artifacts/**/video.webm"),
        "videos/workflow3-configured.webm",
    ).relative_to(ROOT).as_posix()

    store.write_manifest(
        {
            "campaign_id": "evaluation-studio-v1",
            "slice": "workflow3-lifecycle-direct",
            "revision": str(_git(WORKTREE, "rev-parse", "HEAD")).strip(),
            "working_tree_sha256": _tree_digest(),
            "source_sha256": _source_hashes(
                [
                    WORKTREE / "release/live_evaluation/workflow3_lifecycle_evidence.py",
                    WORKTREE / "tests/live_evaluation/test_workflow3_lifecycle_evidence.py",
                    WORKTREE / "frontend/app/lib/runPayload.ts",
                    WORKTREE / "frontend/app/studio/edit/runPayload.test.ts",
                    WORKTREE / "frontend/app/runs/page.test.tsx",
                    WORKTREE / "frontend/app/studio/edit/page.test.tsx",
                ]
            ),
            "database_snapshots": {
                "zeroth_sha256": hashlib.sha256((STATE_ROOT / "zeroth.db").read_bytes()).hexdigest(),
                "action_sink_sha256": hashlib.sha256(
                    (STATE_ROOT / "action-sink/actions.sqlite3").read_bytes()
                ).hexdigest(),
            },
            "runtime": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "frontend_url": "http://127.0.0.1:3000",
                "backend_url": "http://127.0.0.1:8122",
                "browser": "Safari semantic inspection plus Playwright Desktop Chrome rendering",
            },
            "configuration": {
                "tenant_ceiling_usd": 10,
                "run_ceiling_usd": 0.25,
                "deployment_ref": DEPLOYMENT,
                "deployment_version": DEPLOYMENT_VERSION,
                "graph_version_ref": GRAPH,
                "run_id": RUN_ID,
                "approval_id": APPROVAL_ID,
            },
        }
    )

    lifecycle_event = store.append_event(
        "deployment.lifecycle.verified",
        {
            "workflow_observation": paths["workflow"],
            "deployment_observation": paths["deployments"],
            "post_restart_health": paths["health"],
            "result": "pass",
        },
    )
    store.append_event(
        "ui.safari.lifecycle.verified",
        {
            "semantic_state": safari_path,
            "rendered_state": screenshot_path,
            "result": "pass",
        },
        correlation=CorrelationIds(ui_action_id="safari-workflow3-lifecycle-v3"),
    )
    store.append_event(
        "run.sla_expiry.verified",
        {
            "run_observation": paths["run"],
            "approval_observation": paths["approval"],
            "persistence_observation": paths["persistence"],
            "result": "pass",
        },
        correlation=CorrelationIds(run_id=RUN_ID),
    )
    store.append_event(
        "audit.chain.verified",
        {
            "audit_observation": paths["audits"],
            "verification_observation": paths["verification"],
            "result": "pass",
        },
        correlation=CorrelationIds(
            run_id=RUN_ID,
            audit_event_id=f"chain-verification-{RUN_ID}",
        ),
    )

    criteria = _acceptance_criteria(paths, lifecycle_event=lifecycle_event)
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
                    "note": criterion.note,
                }
                for criterion in criteria
            ],
            "artifacts": {
                "axe": axe_path,
                "network": network_path,
                "response_identities": identity_path,
                "video": video_path,
            },
        },
    )
    store.finalize_bundle(
        acceptance=criteria,
        report_markdown=(
            "# Workflow 3 lifecycle direct evidence\n\n"
            "Safari Computer Use directly exercised clone, rename, keyboard save, refresh persistence, "
            "the complete node menu, preflight, publish, deploy, run submission, and navigation-restored "
            "failure state. The collector executed and retained the focused tests, production build, and "
            "Docker restart commands. Post-restart health identifies deployment version 2 serving graph "
            "version 3. The exact run exceeded its five-second approval SLA, was rejected by the SLA "
            "enforcer, retained three valid signed audit records, and created neither a side-effect "
            "operation nor an action marker after the run began.\n\n"
            "This slice does not claim three successful approval repetitions or action-receipt linkage.\n"
        ),
    )


if __name__ == "__main__":
    build()
