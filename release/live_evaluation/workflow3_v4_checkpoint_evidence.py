"""Seal the reviewed Workflow 3 v4 Safari and runtime checkpoint.

The checkpoint preserves direct runtime, audit, persistence, and zero-provider-
cost observations for the exact Safari-submitted run. Screenshot-dependent
campaign criteria remain conservative when native Safari rendered evidence is
missing or captured at the wrong viewport.
"""

# Evidence prose and immutable checkpoint vectors are intentionally verbatim.
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .evidence import AcceptanceCriterion, CorrelationIds, EvidenceStore
from .workflow3_lifecycle_evidence import (
    STATE_ROOT,
    WORKTREE,
    _git,
    _poll_health,
    _request,
    _run_recorded,
    _source_hashes,
    _tree_digest,
    _validate_observations,
    _write_observation,
)

ROOT = STATE_ROOT / "evidence/workflow3-v4-checkpoint-20260824-2"
STAGING_ROOT = STATE_ROOT / "evidence/workflow3-safari-checkpoints-20260824-1-staging"
RUN_ID = "b778fa14a8104527a7a0b78f5bfaef23"
APPROVAL_ID = "c97101a47aa24e6987031d7fb10a4970"
WORKFLOW_ID = "evaluation-studio-v1-governed-remediation"
WORKFLOW_VERSION = 4
DEPLOYMENT = "evaluation-studio-v1-governed-remediation-v2"
DEPLOYMENT_VERSION = 3
GRAPH = "evaluation-studio-v1-governed-remediation@4"

_CREDENTIAL_HEADER_LINE = re.compile(r"(?:X-API-Key|Authorization)\s*:", re.IGNORECASE)


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise RuntimeError("missing checkpoint timestamp")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _checkpoint_criteria(paths: dict[str, str]) -> list[AcceptanceCriterion]:
    visual_note = (
        "Timestamped Safari Computer Use semantics exist, but the required native 1440x900 "
        "checkpoint screenshot is missing or the content pane was omitted by the capture layer."
    )
    sla_note = (
        "The exact SLA behavior, scope joins, signed chain, zero side effects, and zero-provider-"
        "cost reconciliation are durable; full campaign acceptance still requires valid native "
        "Safari result and refresh-restored screenshots."
    )
    return [
        AcceptanceCriterion("ui.node-menu", "not_run", note=visual_note),
        AcceptanceCriterion("ui.keyboard-shortcuts", "not_run", note=visual_note),
        AcceptanceCriterion("ui.publish-deploy-run", "not_run", note=visual_note),
        AcceptanceCriterion(
            "workflow3.health-exact-graph-version",
            "pass",
            (paths["health"], "commands/0004-backend-docker-restart-v4.json"),
        ),
        AcceptanceCriterion(
            "workflow3.publish-deploy-restart",
            "not_run",
            note="Restart and exact health are direct; native publish/deploy checkpoints are incomplete.",
        ),
        AcceptanceCriterion("workflow3.negative-sla-expiry", "not_run", note=sla_note),
    ]


def _sanitize_safari_log(value: Any) -> Any:
    """Remove credential-header-shaped lines while preserving Safari semantics."""
    if isinstance(value, dict):
        return {key: _sanitize_safari_log(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_sanitize_safari_log(child) for child in value]
    if isinstance(value, str):
        lines = value.splitlines()
        sanitized = [
            "[credential header withheld]" if _CREDENTIAL_HEADER_LINE.search(line) else line
            for line in lines
        ]
        return "\n".join(sanitized)
    return value


def _write_sanitized_safari_log(
    store: EvidenceStore, source: Path, destination: str
) -> str:
    value = json.loads(source.read_text())
    sanitized = _sanitize_safari_log(value)
    return store._write_exclusive(Path(destination), sanitized).relative_to(ROOT).as_posix()


def _select_runtime_records() -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    list[dict[str, Any]],
    dict[str, Any],
]:
    health_value = _poll_health()
    workflow_value = _request(f"/api/studio/v1/workflows/{WORKFLOW_ID}")
    deployments_value = _request("/v1/deployments")
    run_value = _request(f"/v1/runs/{RUN_ID}")
    evidence_value = _request(f"/v1/runs/{RUN_ID}/evidence")
    verification_value = _request(f"/v1/runs/{RUN_ID}/verify-chain", method="POST")
    if not all(
        isinstance(value, dict)
        for value in (
            health_value,
            workflow_value,
            run_value,
            evidence_value,
            verification_value,
        )
    ) or not isinstance(deployments_value, list):
        raise RuntimeError("live checkpoint endpoints returned unexpected shapes")
    deployment = next(
        (
            value
            for value in deployments_value
            if isinstance(value, dict)
            and value.get("deployment_ref") == DEPLOYMENT
            and value.get("version") == DEPLOYMENT_VERSION
            and value.get("status") == "active"
        ),
        None,
    )
    approvals = evidence_value.get("approvals")
    audits_value = evidence_value.get("audits")
    if not isinstance(deployment, dict) or not isinstance(approvals, list) or not isinstance(
        audits_value, list
    ):
        raise RuntimeError("deployment, approval, or audit checkpoint evidence is missing")
    approval = next(
        (
            value
            for value in approvals
            if isinstance(value, dict) and value.get("approval_id") == APPROVAL_ID
        ),
        None,
    )
    audits = [value for value in audits_value if isinstance(value, dict)]
    if not isinstance(approval, dict):
        raise RuntimeError("target checkpoint approval is missing")
    return (
        health_value,
        workflow_value,
        deployment,
        run_value,
        audits,
        {"approval": approval, "verification": verification_value},
    )


def _persistence_and_economics(
    *, approval: dict[str, Any], audits: list[dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, Any]]:
    with sqlite3.connect(STATE_ROOT / "zeroth.db") as database:
        side_effect_count = int(
            database.execute(
                "SELECT COUNT(*) FROM side_effect_operations WHERE run_id = ?", (RUN_ID,)
            ).fetchone()[0]
        )
    created = _parse_timestamp(approval.get("created_at"))
    with sqlite3.connect(STATE_ROOT / "action-sink/actions.sqlite3") as sink:
        marker_times = [
            row[0] for row in sink.execute("SELECT created_at FROM action_markers").fetchall()
        ]
    marker_count_since_run = sum(
        _parse_timestamp(value) >= created for value in marker_times if isinstance(value, str)
    )

    with sqlite3.connect(STATE_ROOT / "econ.db") as economics:
        economics.row_factory = sqlite3.Row
        execution_rows = economics.execute(
            """
            SELECT execution_id, join_key, token_cost_usd, tool_cost_usd, compute_cost_usd,
                   cost_measurement, usage_measurement, campaign_id, operation_id,
                   provider_request_id, deployment_ref, evidence_kind
            FROM execution_events WHERE execution_id = ? OR join_key = ?
            """,
            (RUN_ID, RUN_ID),
        ).fetchall()
        reservation_rows = economics.execute(
            """
            SELECT operation_id, campaign_id, run_id, status, max_cost_usd, held_cost_usd,
                   actual_cost_usd, released_cost_usd, cost_measurement, cost_event_id,
                   provider_request_id, cleanup_status, deployment_ref, evidence_kind
            FROM cost_reservations WHERE run_id = ?
            """,
            (RUN_ID,),
        ).fetchall()

    audit_costs = [float(audit.get("cost_usd") or 0.0) for audit in audits]
    provider_calls = sum(
        1
        for audit in audits
        if audit.get("cost_event_id") is not None
        or audit.get("provider") is not None
        or audit.get("model") is not None
    )
    event_cost = sum(
        float(row["token_cost_usd"] or 0)
        + float(row["tool_cost_usd"] or 0)
        + float(row["compute_cost_usd"] or 0)
        for row in execution_rows
    )
    economics_observation = {
        "run_id": RUN_ID,
        "provider_call_count": provider_calls,
        "execution_event_count": len(execution_rows),
        "reservation_count": len(reservation_rows),
        "audit_cost_usd": sum(audit_costs),
        "execution_event_cost_usd": event_cost,
        "cost_identity_disposition": "not_applicable_provider_free_path",
        "one_event_per_noncache_provider_call": provider_calls == len(execution_rows) == 0,
        "reconciled": sum(audit_costs) == event_cost == 0.0,
        "execution_events": [dict(row) for row in execution_rows],
        "reservations": [dict(row) for row in reservation_rows],
    }
    persistence_observation = {
        "run_id": RUN_ID,
        "side_effect_operation_rows": side_effect_count,
        "action_markers_created_since_run": marker_count_since_run,
        "invariant": "approval expired before any side-effect operation or action marker",
    }
    return persistence_observation, economics_observation


def _sanitize_audits(audits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fields = (
        "audit_id",
        "run_id",
        "thread_id",
        "node_id",
        "graph_version_ref",
        "deployment_ref",
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
        "cost_usd",
        "cost_measurement",
        "cost_event_id",
        "started_at",
        "completed_at",
    )
    return [{field: audit.get(field) for field in fields} for audit in audits]


def build() -> None:
    store = EvidenceStore(ROOT)
    if store.is_sealed or any(ROOT.iterdir()):
        raise RuntimeError(f"evidence root is not fresh: {ROOT}")

    _run_recorded(
        store,
        sequence=1,
        name="workflow3-scope-and-collector-tests",
        argv=[
            "uv",
            "run",
            "pytest",
            "-q",
            "tests/live_evaluation/test_workflow3_lifecycle_evidence.py",
            "tests/test_approval_sla.py",
            "tests/approvals/test_service.py",
        ],
        cwd=WORKTREE,
    )
    _run_recorded(
        store,
        sequence=2,
        name="frontend-workflow3-tests",
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
        sequence=3,
        name="frontend-production-build",
        argv=["npm", "run", "build"],
        cwd=WORKTREE / "frontend",
    )
    restart_environment = dict(os.environ)
    restart_environment["ZEROTH_DEV_DEPLOYMENT_REF"] = DEPLOYMENT
    _run_recorded(
        store,
        sequence=4,
        name="backend-docker-restart-v4",
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

    health, workflow, deployment, run, audits, joined = _select_runtime_records()
    approval = joined["approval"]
    verification = joined["verification"]
    persistence, economics = _persistence_and_economics(approval=approval, audits=audits)
    _validate_observations(
        workflow=workflow,
        deployment=deployment,
        health=health,
        run=run,
        approval=approval,
        audits=audits,
        verification=verification,
        side_effect_count=int(persistence["side_effect_operation_rows"]),
        marker_count_since_run=int(persistence["action_markers_created_since_run"]),
        expected_workflow_version=WORKFLOW_VERSION,
        expected_deployment_version=DEPLOYMENT_VERSION,
        expected_graph=GRAPH,
        expected_run_id=RUN_ID,
        expected_approval_id=APPROVAL_ID,
    )
    if not economics["reconciled"] or not economics["one_event_per_noncache_provider_call"]:
        raise RuntimeError("provider-free run economics did not reconcile to zero")

    paths = {
        "health": _write_observation(store, "health-v4", health),
        "workflow": _write_observation(
            store,
            "workflow-published-v4",
            {
                "id": workflow.get("id"),
                "name": workflow.get("name"),
                "status": workflow.get("status"),
                "version": workflow.get("version"),
                "entry_step": workflow.get("entry_step"),
                "node_ids": [
                    value.get("id") for value in workflow.get("nodes", []) if isinstance(value, dict)
                ],
                "edge_ids": [
                    value.get("id") for value in workflow.get("edges", []) if isinstance(value, dict)
                ],
            },
        ),
        "deployment": _write_observation(store, "deployment-active-v3", deployment),
        "run": _write_observation(
            store,
            "run-sla-terminal",
            {
                field: run.get(field)
                for field in (
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
            },
        ),
        "approval": _write_observation(
            store,
            "approval-sla-scope",
            {
                field: approval.get(field)
                for field in (
                    "approval_id",
                    "run_id",
                    "thread_id",
                    "node_id",
                    "graph_version_ref",
                    "deployment_ref",
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
            },
        ),
        "audits": _write_observation(store, "signed-audit-scope", _sanitize_audits(audits)),
        "verification": _write_observation(store, "chain-verification", verification),
        "persistence": _write_observation(store, "side-effect-absence", persistence),
        "economics": _write_observation(store, "zero-provider-cost-reconciliation", economics),
    }

    ingested = {
        "keyboard_save": store.ingest_artifact(
            STAGING_ROOT / "screenshots/02-keyboard-save.jpeg",
            "screenshots/keyboard-save-provisional.jpeg",
        ).relative_to(ROOT).as_posix(),
        "lifecycle_actions": _write_sanitized_safari_log(
            store,
            STAGING_ROOT / "accessibility/safari-actions-v4.json",
            "accessibility/safari-actions-v4.json",
        ),
        "sla_actions": _write_sanitized_safari_log(
            store,
            STAGING_ROOT / "accessibility/safari-sla-scope-fixed-v4.json",
            "accessibility/safari-sla-scope-fixed-v4.json",
        ),
        "runs_actions": _write_sanitized_safari_log(
            store,
            STAGING_ROOT / "accessibility/safari-runs-audit-fixed-v4.json",
            "accessibility/safari-runs-audit-fixed-v4.json",
        ),
    }

    store.write_manifest(
        {
            "campaign_id": "evaluation-studio-v1",
            "slice": "workflow3-v4-safari-runtime-checkpoint",
            "revision": str(_git(WORKTREE, "rev-parse", "HEAD")).strip(),
            "working_tree_sha256": _tree_digest(),
            "source_sha256": _source_hashes(
                [
                    WORKTREE / "release/live_evaluation/workflow3_v4_checkpoint_evidence.py",
                    WORKTREE / "release/live_evaluation/workflow3_lifecycle_evidence.py",
                    WORKTREE / "tests/live_evaluation/test_workflow3_lifecycle_evidence.py",
                    WORKTREE / "src/zeroth/governance/approvals/service.py",
                    WORKTREE / "tests/test_approval_sla.py",
                ]
            ),
            "database_hashes": {
                "zeroth_sha256": hashlib.sha256((STATE_ROOT / "zeroth.db").read_bytes()).hexdigest(),
                "econ_sha256": hashlib.sha256((STATE_ROOT / "econ.db").read_bytes()).hexdigest(),
                "action_sink_sha256": hashlib.sha256(
                    (STATE_ROOT / "action-sink/actions.sqlite3").read_bytes()
                ).hexdigest(),
            },
            "runtime": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "frontend_url": "http://127.0.0.1:3000",
                "backend_url": "http://127.0.0.1:8122",
                "browser": "Safari operated through macOS Computer Use",
                "safari_capture_viewport": "1223x768",
            },
            "configuration": {
                "tenant_ceiling_usd": 10,
                "run_ceiling_usd": 0.25,
                "workflow_id": WORKFLOW_ID,
                "workflow_version": WORKFLOW_VERSION,
                "deployment_ref": DEPLOYMENT,
                "deployment_version": DEPLOYMENT_VERSION,
                "graph_version_ref": GRAPH,
                "run_id": RUN_ID,
                "approval_id": APPROVAL_ID,
            },
            "limitations": [
                "Native Safari checkpoint captures are not 1440x900.",
                "The Safari capture layer omitted the content pane after refresh and on Runs.",
                "The path is provider-free, so no product cost-event identity is expected or present.",
            ],
        }
    )

    health_event = store.append_event(
        "runtime.health.observed",
        {"evidence_path": paths["health"], "result": "pass"},
    )
    store.append_event(
        "run.sla_expiry.observed",
        {
            "approval_evidence": paths["approval"],
            "persistence_evidence": paths["persistence"],
            "result": "behaviorally_verified_not_campaign_accepted",
        },
        correlation=CorrelationIds(run_id=RUN_ID),
    )
    store.append_event(
        "audit.chain.verified",
        {
            "audit_evidence": paths["audits"],
            "verification_evidence": paths["verification"],
            "result": "pass",
        },
        correlation=CorrelationIds(
            run_id=RUN_ID, audit_event_id=f"chain-verification:{RUN_ID}"
        ),
    )
    store.append_event(
        "reconciliation.zero_provider_calls.observed",
        {"evidence_path": paths["economics"], "result": "pass"},
        correlation=CorrelationIds(run_id=RUN_ID),
    )
    store.append_event(
        "ui.safari.checkpoint.observed",
        {
            "lifecycle_actions": ingested["lifecycle_actions"],
            "sla_actions": ingested["sla_actions"],
            "runs_actions": ingested["runs_actions"],
            "keyboard_screenshot": ingested["keyboard_save"],
            "result": "provisional_not_campaign_accepted",
        },
        correlation=CorrelationIds(
            run_id=RUN_ID, ui_action_id="safari-workflow3-v4-checkpoint"
        ),
    )

    criteria = _checkpoint_criteria(paths)
    store._write_exclusive(
        Path("results.json"),
        {
            "schema_version": 1,
            "completed": True,
            "criteria": [
                {
                    "criterion_id": item.criterion_id,
                    "status": item.status,
                    "evidence": list(item.evidence),
                    "note": item.note,
                }
                for item in criteria
            ],
            "artifacts": ingested,
            "health_event": health_event,
        },
    )
    store.finalize_bundle(
        acceptance=criteria,
        report_markdown=(
            "# Workflow 3 v4 Safari/runtime checkpoint\n\n"
            "Safari Computer Use submitted the exact v4 run, observed its approval pause, refreshed "
            "the page after SLA rejection, opened the run in Runs, and invoked signed-chain "
            "verification. Direct runtime observations prove the run, approval, SLA actor, and all "
            "audit records share tenant `evaluation-studio-v1` and workspace `null`; the five-second "
            "deadline was exceeded before rejection. The three-record signed chain verifies and the "
            "run created no side-effect operation or action marker.\n\n"
            "This approval-only path made zero provider calls. Audit and economics execution-event "
            "totals both reconcile to zero; therefore no provider request or product cost-event "
            "identity is applicable.\n\n"
            "The bundle does not accept node-menu, keyboard, publish/deploy/run, or SLA screenshot "
            "criteria. The available keyboard screenshot is 1223x768 and subsequent Safari captures "
            "omit the populated content pane, so the required native 1440x900 visual checkpoints "
            "remain incomplete.\n"
        ),
    )


if __name__ == "__main__":
    build()
