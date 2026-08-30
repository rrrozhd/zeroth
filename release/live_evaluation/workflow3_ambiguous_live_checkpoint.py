"""Direct, fail-closed Workflow 3 ambiguous-outcome live checkpoint."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sqlite3
import subprocess
import time
from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx

from .action_runner import EVALUATION_ACTION_MANIFEST_SHA256
from .evidence import AcceptanceCriterion, CorrelationIds, EvidenceStore
from .runtime_paths import resolve_runtime_paths

_RUNTIME_PATHS = resolve_runtime_paths()
WORKTREE = _RUNTIME_PATHS.worktree
STATE_ROOT = _RUNTIME_PATHS.state_root
SERVICE_KEY_PATH = STATE_ROOT / "runtime-secrets/service-api-key"
MAIN_DB = STATE_ROOT / "zeroth.db"
SINK_DB = STATE_ROOT / "action-sink/actions.sqlite3"
FAULT_DB = STATE_ROOT / "fault-control.sqlite3"
BASE_URL = "http://127.0.0.1:8122"
TENANT = "evaluation-studio-v1"
DEPLOYMENT = "evaluation-studio-v1-governed-remediation-v2"
GRAPH = "evaluation-studio-v1-governed-remediation@4"
DEPLOYMENT_VERSION = 3


def _nested(value: Mapping[str, Any], *path: str) -> Any:
    current: Any = value
    for field in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(field)
    return current


def validate_proof(proof: Mapping[str, Any]) -> None:
    """Accept only the exact one-effect, one-lookup, operator-resolved lifecycle."""
    health = proof.get("health")
    if not isinstance(health, Mapping) or (
        health.get("status"),
        health.get("deployment_ref"),
        health.get("deployment_version"),
        health.get("graph_version_ref"),
        health.get("campaign_id"),
    ) != ("ok", DEPLOYMENT, DEPLOYMENT_VERSION, GRAPH, TENANT):
        raise RuntimeError("live checkpoint does not serve exact Workflow 3 v4")
    run_id = proof.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise RuntimeError("live checkpoint lacks a fresh run identity")
    approval = proof.get("approval")
    if not isinstance(approval, Mapping) or (
        approval.get("status"),
        approval.get("decision"),
    ) != ("resolved", "approve"):
        raise RuntimeError("live checkpoint approval was not explicitly approved")
    marker = proof.get("marker")
    if not isinstance(marker, Mapping):
        raise RuntimeError("live checkpoint lacks action-marker evidence")
    baseline = marker.get("baseline")
    after_commit = marker.get("after_commit")
    final = marker.get("final")
    if not all(isinstance(value, int) for value in (baseline, after_commit, final)):
        raise RuntimeError("action-marker counts are malformed")
    if after_commit != baseline + 1 or final != after_commit:
        raise RuntimeError("ambiguous lifecycle did not create exactly one marker")
    operation = proof.get("operation")
    if not isinstance(operation, Mapping) or not isinstance(operation.get("operation_key"), str):
        raise RuntimeError("side-effect operation identity is missing")
    expected_snapshots = {
        "after_timeout": ("AMBIGUOUS", 0),
        "after_lookup": ("AMBIGUOUS", 1),
        "after_refusal": ("AMBIGUOUS", 1),
        "after_resolution": ("COMPLETED", 1),
    }
    for name, expected in expected_snapshots.items():
        if (
            _nested(operation, name, "state"),
            _nested(operation, name, "reconciliation_attempts"),
        ) != expected:
            raise RuntimeError(f"operation checkpoint {name} violates fail-closed lifecycle")
    fault = proof.get("fault")
    if not isinstance(fault, Mapping) or (
        fault.get("target"),
        fault.get("mode"),
        fault.get("consumed"),
    ) != ("action_outcome_lookup", "unavailable", True):
        raise RuntimeError("dedicated one-shot lookup fault was not consumed")
    attempts = proof.get("attempts")
    if not isinstance(attempts, Mapping) or (
        attempts.get("action_first_execution_count"),
        attempts.get("authoritative_lookup_attempt_count"),
        attempts.get("automatic_reexecution_count"),
    ) != (1, 1, 0):
        raise RuntimeError("action/lookup/reexecution cardinality is not exact")
    refusal = proof.get("dispatch_refusal")
    if not isinstance(refusal, Mapping) or (
        refusal.get("first_public_status"),
        refusal.get("second_http_status"),
        refusal.get("final_public_status"),
    ) != ("waiting_interrupt", 409, "waiting_interrupt"):
        raise RuntimeError("durable reconciliation pause/refusal sequence is incomplete")
    resolution = proof.get("resolution")
    if not isinstance(resolution, Mapping) or (
        resolution.get("http_status"),
        resolution.get("state"),
        resolution.get("signed_audit_count"),
    ) != (200, "COMPLETED", 1):
        raise RuntimeError("authorized signed operator resolution is incomplete")
    chain = proof.get("chain")
    if not isinstance(chain, Mapping) or not (
        chain.get("verified") is True
        and chain.get("signature_verified") is True
        and chain.get("unsigned_record_count") == 0
    ):
        raise RuntimeError("final signed audit chain did not verify")


class _Api:
    def __init__(self) -> None:
        key = SERVICE_KEY_PATH.read_text(encoding="utf-8").strip()
        if not key:
            raise RuntimeError("persistent service credential is missing")
        self.client = httpx.Client(
            base_url=BASE_URL,
            headers={"X-API-Key": key, "X-Tenant-ID": TENANT},
            timeout=15,
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        expected: set[int] | None = None,
    ) -> tuple[int, Any]:
        response = self.client.request(method, path, json=body)
        if response.status_code not in (expected or {200}):
            raise RuntimeError(
                f"supported runtime boundary {method} {path} returned {response.status_code}"
            )
        if response.status_code == 204 or not response.content:
            return response.status_code, None
        try:
            value = response.json()
        except ValueError as exc:
            raise RuntimeError(f"runtime response from {path} is not JSON") from exc
        return response.status_code, value


def _read_rows(path: Path, query: str, parameters: tuple[object, ...] = ()) -> list[dict[str, Any]]:
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as database:
        database.row_factory = sqlite3.Row
        return [dict(row) for row in database.execute(query, parameters).fetchall()]


def _marker_count() -> int:
    rows = _read_rows(SINK_DB, "SELECT COUNT(*) AS count FROM action_markers")
    return int(rows[0]["count"])


def _operation(run_id: str) -> dict[str, Any]:
    rows = _read_rows(
        MAIN_DB,
        """
        SELECT operation_key, run_id, target_ref, state, reconciliation_attempts,
               ambiguity_reason, error, created_at, updated_at
        FROM side_effect_operations
        WHERE tenant_id = ? AND run_id = ?
        """,
        (TENANT, run_id),
    )
    if len(rows) != 1:
        raise RuntimeError(f"expected one side-effect operation for {run_id}; found {len(rows)}")
    row = rows[0]
    return {
        "operation_key": row["operation_key"],
        "run_id": row["run_id"],
        "target_ref_sha256": hashlib.sha256(str(row["target_ref"]).encode()).hexdigest(),
        "state": row["state"],
        "reconciliation_attempts": int(row["reconciliation_attempts"]),
        "ambiguity_reason_sha256": (
            None
            if row["ambiguity_reason"] is None
            else hashlib.sha256(str(row["ambiguity_reason"]).encode()).hexdigest()
        ),
        "error_sha256": (
            None if row["error"] is None else hashlib.sha256(str(row["error"]).encode()).hexdigest()
        ),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _marker(operation_key: str) -> dict[str, Any]:
    rows = _read_rows(
        SINK_DB,
        """
        SELECT operation_key, payload_hash, receipt, created_at
        FROM action_markers WHERE operation_key = ?
        """,
        (operation_key,),
    )
    if len(rows) != 1:
        raise RuntimeError("authoritative action marker is missing or duplicated")
    row = rows[0]
    return {
        "operation_key": row["operation_key"],
        "payload_hash": row["payload_hash"],
        "receipt": row["receipt"],
        "created_at": row["created_at"],
    }


def _fault() -> dict[str, Any]:
    rows = _read_rows(
        FAULT_DB,
        """
        SELECT fault_id, campaign_id, target, mode, armed_at, consumed_at
        FROM evaluation_faults
        WHERE campaign_id = ? AND target = 'action_outcome_lookup'
        ORDER BY armed_at DESC, fault_id DESC LIMIT 1
        """,
        (TENANT,),
    )
    if len(rows) != 1:
        raise RuntimeError("dedicated lookup fault record is missing")
    row = rows[0]
    return {
        "fault_id": row["fault_id"],
        "campaign_id": row["campaign_id"],
        "target": row["target"],
        "mode": row["mode"],
        "armed_at": row["armed_at"],
        "consumed_at": row["consumed_at"],
        "consumed": row["consumed_at"] is not None,
    }


def _safe_run(value: Mapping[str, Any]) -> dict[str, Any]:
    failure = value.get("failure_state")
    return {
        field: value.get(field)
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
        )
    } | {
        "failure_reason": failure.get("reason") if isinstance(failure, Mapping) else None,
        "terminal_output_present": isinstance(value.get("terminal_output"), Mapping),
    }


def _wait_run(api: _Api, run_id: str, *, expected: set[str], timeout: float = 25) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _status, value = api.request("GET", f"/v1/runs/{quote(run_id, safe='')}")
        if isinstance(value, dict) and value.get("status") in expected:
            return value
        time.sleep(0.05)
    raise RuntimeError(f"run {run_id} did not reach {sorted(expected)}")


def _wait_operation(
    run_id: str, *, state: str, attempts: int, timeout: float = 15
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            value = _operation(run_id)
            if (value["state"], value["reconciliation_attempts"]) == (state, attempts):
                return value
        except RuntimeError as exc:
            last_error = exc
        time.sleep(0.05)
    raise RuntimeError(f"operation did not reach {state}/{attempts}") from last_error


def _approve(api: _Api, run_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        _status, evidence = api.request("GET", f"/v1/runs/{quote(run_id, safe='')}/evidence")
        approvals = evidence.get("approvals") if isinstance(evidence, dict) else None
        pending = [
            item
            for item in approvals or []
            if isinstance(item, dict) and item.get("status") == "pending"
        ]
        if len(pending) == 1 and isinstance(pending[0].get("approval_id"), str):
            approval_id = str(pending[0]["approval_id"])
            _code, resolved = api.request(
                "POST",
                (f"/v1/deployments/{DEPLOYMENT}/approvals/{quote(approval_id, safe='')}/resolve"),
                body={"decision": "approve", "edited_payload": None},
            )
            if not isinstance(resolved, dict):
                raise RuntimeError("approval resolution response is malformed")
            return {
                "approval_id": approval_id,
                "status": resolved.get("status"),
                "decision": _nested(resolved, "resolution", "decision"),
            }
        time.sleep(0.03)
    raise RuntimeError("fresh Workflow 3 approval was not available before its SLA")


def _replay(api: _Api, run_id: str) -> dict[str, Any]:
    _code, queued = api.request("POST", f"/v1/admin/runs/{quote(run_id, safe='')}/replay")
    if not isinstance(queued, dict) or queued.get("status") != "queued":
        raise RuntimeError("failed run replay was not durably requeued")
    return _wait_run(api, run_id, expected={"waiting_interrupt"})


def _assert_replay_refused(api: _Api, run_id: str) -> dict[str, Any]:
    code, _body = api.request(
        "POST",
        f"/v1/admin/runs/{quote(run_id, safe='')}/replay",
        expected={409},
    )
    return {
        "http_status": code,
        "run": _wait_run(api, run_id, expected={"waiting_interrupt"}),
    }


def _audits(api: _Api, run_id: str) -> list[dict[str, Any]]:
    _status, evidence = api.request("GET", f"/v1/runs/{quote(run_id, safe='')}/evidence")
    values = evidence.get("audits") if isinstance(evidence, dict) else None
    if not isinstance(values, list):
        raise RuntimeError("run evidence lacks audit records")
    result: list[dict[str, Any]] = []
    allowed_metadata = {
        "manifest_ref_sha256",
        "operation_key",
        "operation_state",
        "operation_first_execution",
        "operation_reconciliation_required",
        "operation_reconciliation_exhausted",
        "operation_replay_suppressed",
        "resolution_reason_sha256",
        "receipt_sha256",
    }
    for value in values:
        if not isinstance(value, dict):
            raise RuntimeError("run evidence contains a malformed audit record")
        metadata = value.get("execution_metadata")
        safe_metadata = (
            {field: metadata.get(field) for field in allowed_metadata if field in metadata}
            if isinstance(metadata, dict)
            else {}
        )
        result.append(
            {
                field: value.get(field)
                for field in (
                    "audit_id",
                    "run_id",
                    "node_id",
                    "graph_version_ref",
                    "deployment_ref",
                    "tenant_id",
                    "workspace_id",
                    "status",
                    "chain_sequence",
                    "previous_record_digest",
                    "record_digest",
                    "record_signature",
                    "signing_key_id",
                    "signing_algorithm",
                    "cost_usd",
                    "cost_event_id",
                )
            }
            | {"execution_metadata": safe_metadata}
        )
    return result


def _count_attempts(audits: list[dict[str, Any]], lookup_attempts: int) -> dict[str, int]:
    action = [
        row
        for row in audits
        if _nested(row, "execution_metadata", "manifest_ref_sha256")
        == EVALUATION_ACTION_MANIFEST_SHA256
    ]
    first = sum(
        _nested(row, "execution_metadata", "operation_first_execution") is True for row in action
    )
    if first == 0:
        # Older action audit projections omit the explicit boolean. A signed
        # manifest-backed action record is itself durable execution evidence;
        # replay-refusal history records carry no manifest hash.
        first = sum(bool(row.get("record_signature")) for row in action)
    return {
        "action_first_execution_count": first,
        "authoritative_lookup_attempt_count": lookup_attempts,
        "automatic_reexecution_count": max(0, first - 1),
        "action_audit_count": len(action),
    }


def _initial_ambiguous_snapshot(
    audits: list[dict[str, Any]], operation_key: str
) -> dict[str, Any]:
    matches = [
        row
        for row in audits
        if row.get("status") == "failed"
        and bool(row.get("record_signature"))
        and _nested(row, "execution_metadata", "manifest_ref_sha256")
        == EVALUATION_ACTION_MANIFEST_SHA256
        and _nested(row, "execution_metadata", "operation_key") == operation_key
        and _nested(row, "execution_metadata", "operation_state") == "ambiguous"
    ]
    if len(matches) != 1 or not isinstance(matches[0].get("audit_id"), str):
        raise RuntimeError("signed initial ambiguous action audit is missing or duplicated")
    return {
        "state": "AMBIGUOUS",
        "reconciliation_attempts": 0,
        "signed_audit_id": matches[0]["audit_id"],
    }


def _approved_snapshot(audits: list[dict[str, Any]]) -> dict[str, str]:
    matches = [
        row
        for row in audits
        if row.get("node_id") == "approval"
        and row.get("status") == "approval_api"
        and bool(row.get("record_signature"))
        and isinstance(row.get("audit_id"), str)
        and str(row["audit_id"]).startswith("approval-api:")
        and str(row["audit_id"]).endswith(":approve")
    ]
    if len(matches) != 1:
        raise RuntimeError("signed explicit approval audit is missing or duplicated")
    audit_id = str(matches[0]["audit_id"])
    approval_id = audit_id.removeprefix("approval-api:").removesuffix(":approve")
    if not approval_id:
        raise RuntimeError("signed explicit approval audit lacks approval identity")
    return {
        "approval_id": approval_id,
        "status": "resolved",
        "decision": "approve",
        "signed_audit_id": audit_id,
    }


def _run_command(argv: list[str], *, env: dict[str, str] | None = None) -> dict[str, Any]:
    result = subprocess.run(
        argv,
        cwd=WORKTREE,
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )
    record = {
        "argv": argv,
        "working_directory": str(WORKTREE),
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
    if result.returncode:
        raise RuntimeError(f"checkpoint command failed: {argv[0]}")
    return record


def collect_live() -> dict[str, Any]:
    """Execute one fresh run through supported loopback APIs and read-only DB queries."""
    if _read_rows(
        FAULT_DB,
        """
        SELECT fault_id FROM evaluation_faults
        WHERE campaign_id = ? AND target = 'action_outcome_lookup' AND consumed_at IS NULL
        """,
        (TENANT,),
    ):
        raise RuntimeError("an active lookup fault already exists; refusing to steal it")
    commands = [
        _run_command(
            [
                "uv",
                "run",
                "pytest",
                "-q",
                "tests/live_evaluation/test_action_runner.py",
                "-k",
                "campaign_lookup_fault or timeout_lookup_outage",
                "tests/live_evaluation/test_workflow3_ambiguous_live_checkpoint.py",
            ]
        )
    ]
    restart_env = dict(os.environ)
    restart_env["ZEROTH_DEV_DEPLOYMENT_REF"] = DEPLOYMENT
    commands.append(
        _run_command(
            [
                "docker",
                "compose",
                "-f",
                "compose.dev.yml",
                "up",
                "-d",
                "--force-recreate",
                "backend",
            ],
            env=restart_env,
        )
    )
    api = _Api()
    health: dict[str, Any] | None = None
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            _status, candidate = api.request("GET", "/health")
            if isinstance(candidate, dict) and candidate.get("status") == "ok":
                health = candidate
                break
        except (httpx.HTTPError, RuntimeError):
            pass
        time.sleep(0.25)
    if health is None:
        raise RuntimeError("backend did not recover after supported recreation")
    if (
        health.get("deployment_ref"),
        health.get("deployment_version"),
        health.get("graph_version_ref"),
    ) != (DEPLOYMENT, DEPLOYMENT_VERSION, GRAPH):
        raise RuntimeError("backend recreation did not serve exact Workflow 3 v4")
    _status, openapi = api.request("GET", "/openapi.json")
    if not isinstance(openapi, dict) or "/faults/arm" not in openapi.get("paths", {}):
        raise RuntimeError("supported campaign fault-control boundary is absent")

    baseline = _marker_count()
    external_operation_id = f"live-ambiguous-{uuid4().hex}"
    api.request(
        "POST",
        "/faults/arm",
        body={
            "campaign_id": TENANT,
            "operation_id": external_operation_id,
            "run_id": "server-response:run_id",
            "deterministic": True,
            "target": "action_outcome_lookup",
            "mode": "unavailable",
            "parameters": {},
        },
        expected={204},
    )
    ticket = f"synthetic-ambiguous-live-{uuid4().hex[:12]}"
    _code, created = api.request(
        "POST",
        "/v1/runs",
        body={
            "campaign_id": TENANT,
            "campaign_strict": True,
            "input_payload": {
                "ticket": ticket,
                "status": "remediated",
                "fault": "timeout_after_commit",
            },
        },
        expected={200, 202},
    )
    if not isinstance(created, dict) or not isinstance(created.get("run_id"), str):
        raise RuntimeError("run submission omitted its runtime identity")
    run_id = str(created["run_id"])
    approval = _approve(api, run_id)
    first_failed = _wait_run(api, run_id, expected={"failed"})
    after_timeout = _wait_operation(run_id, state="AMBIGUOUS", attempts=0)
    marker = _marker(str(after_timeout["operation_key"]))
    if _marker_count() != baseline + 1:
        raise RuntimeError("timeout-after-commit did not create exactly one marker")

    first_refusal = _replay(api, run_id)
    after_lookup = _wait_operation(run_id, state="AMBIGUOUS", attempts=1)
    fault = _fault()
    if fault["consumed"] is not True:
        raise RuntimeError("automatic outcome lookup did not consume its one-shot fault")

    second_refusal = _assert_replay_refused(api, run_id)
    after_refusal = _wait_operation(run_id, state="AMBIGUOUS", attempts=1)
    if _marker_count() != baseline + 1:
        raise RuntimeError("automatic refusal reexecuted the external action")

    receipt_payload = {
        "operation_key": marker["operation_key"],
        "payload_hash": marker["payload_hash"],
        "receipt": marker["receipt"],
        "created_at": marker["created_at"],
    }
    reason = "authorized live evaluation reconciliation from authoritative local receipt"
    resolution_status, resolution = api.request(
        "POST",
        (
            f"/v1/deployments/{DEPLOYMENT}/operations/"
            f"{quote(str(marker['operation_key']), safe='')}/resolve"
        ),
        body={"resolution": "completed", "reason": reason, "receipt": receipt_payload},
    )
    if not isinstance(resolution, dict):
        raise RuntimeError("operator resolution response is malformed")
    after_resolution = _wait_operation(run_id, state="COMPLETED", attempts=1)
    final_run = _wait_run(api, run_id, expected={"waiting_interrupt"})
    final_marker_count = _marker_count()
    audits = _audits(api, run_id)
    resolution_audits = [
        row
        for row in audits
        if row.get("node_id") == "operation.resolve"
        and row.get("record_signature")
        and _nested(row, "execution_metadata", "operation_key") == marker["operation_key"]
    ]
    _status, chain = api.request("POST", f"/v1/runs/{quote(run_id, safe='')}/verify-chain")
    if not isinstance(chain, dict):
        raise RuntimeError("audit verification response is malformed")
    attempts = _count_attempts(audits, int(after_refusal["reconciliation_attempts"]))
    proof = {
        "schema_version": 1,
        "checkpoint": "workflow3-ambiguous-no-reexecution-direct-live",
        "created_at": datetime.now(UTC).isoformat(),
        "health": health,
        "run_id": run_id,
        "approval": approval,
        "marker": {
            "baseline": baseline,
            "after_commit": baseline + 1,
            "final": final_marker_count,
            "operation_key": marker["operation_key"],
            "payload_hash": marker["payload_hash"],
            "receipt_sha256": hashlib.sha256(str(marker["receipt"]).encode()).hexdigest(),
            "created_at": marker["created_at"],
        },
        "operation": {
            "operation_key": marker["operation_key"],
            "after_timeout": after_timeout,
            "after_lookup": after_lookup,
            "after_refusal": after_refusal,
            "after_resolution": after_resolution,
        },
        "fault": fault,
        "attempts": attempts,
        "dispatch_refusal": {
            "first_public_status": first_refusal.get("status"),
            "second_http_status": second_refusal.get("http_status"),
            "final_public_status": final_run.get("status"),
        },
        "runs": {
            "after_timeout": _safe_run(first_failed),
            "after_lookup": _safe_run(first_refusal),
            "after_refusal": _safe_run(second_refusal["run"]),
            "after_resolution": _safe_run(final_run),
        },
        "resolution": {
            "http_status": resolution_status,
            "state": resolution.get("state"),
            "reason_sha256": hashlib.sha256(reason.encode()).hexdigest(),
            "signed_audit_count": len(resolution_audits),
        },
        "chain": {
            field: chain.get(field)
            for field in (
                "verified",
                "signature_verified",
                "record_count",
                "unsigned_record_count",
            )
        },
        "audits": audits,
        "commands": commands,
        "ui": {"claimed": False, "reason": "not inspected yet"},
    }
    validate_proof(proof)
    return proof


def complete_paused_live(run_id: str) -> dict[str, Any]:
    """Complete an already-paused checkpoint without re-arming or re-executing it."""
    commands = [
        _run_command(
            [
                "uv",
                "run",
                "pytest",
                "-q",
                "tests/live_evaluation/test_workflow3_ambiguous_live_checkpoint.py",
            ]
        )
    ]
    api = _Api()
    _status, health = api.request("GET", "/health")
    if not isinstance(health, dict):
        raise RuntimeError("health response is malformed")
    current_run = _wait_run(api, run_id, expected={"waiting_interrupt"})
    after_lookup = _wait_operation(run_id, state="AMBIGUOUS", attempts=1)
    operation_key = str(after_lookup["operation_key"])
    marker = _marker(operation_key)
    marker_row = _read_rows(
        SINK_DB,
        "SELECT rowid AS marker_rowid FROM action_markers WHERE operation_key = ?",
        (operation_key,),
    )
    if len(marker_row) != 1:
        raise RuntimeError("checkpoint marker row identity is missing or duplicated")
    baseline = int(
        _read_rows(
            SINK_DB,
            "SELECT COUNT(*) AS count FROM action_markers WHERE rowid < ?",
            (marker_row[0]["marker_rowid"],),
        )[0]["count"]
    )
    fault = _fault()
    if fault["consumed"] is not True:
        raise RuntimeError("checkpoint lookup fault was not consumed")
    audits_before = _audits(api, run_id)
    after_timeout = _initial_ambiguous_snapshot(audits_before, operation_key)
    approval = _approved_snapshot(audits_before)

    second_refusal = _assert_replay_refused(api, run_id)
    after_refusal = _wait_operation(run_id, state="AMBIGUOUS", attempts=1)
    if _marker_count() != baseline + 1:
        raise RuntimeError("repeated replay refusal changed the action-marker count")

    receipt_payload = {
        "operation_key": marker["operation_key"],
        "payload_hash": marker["payload_hash"],
        "receipt": marker["receipt"],
        "created_at": marker["created_at"],
    }
    reason = "authorized live evaluation reconciliation from authoritative local receipt"
    resolution_status, resolution = api.request(
        "POST",
        (
            f"/v1/deployments/{DEPLOYMENT}/operations/"
            f"{quote(operation_key, safe='')}/resolve"
        ),
        body={"resolution": "completed", "reason": reason, "receipt": receipt_payload},
    )
    if not isinstance(resolution, dict):
        raise RuntimeError("operator resolution response is malformed")
    after_resolution = _wait_operation(run_id, state="COMPLETED", attempts=1)
    final_run = _wait_run(api, run_id, expected={"waiting_interrupt"})
    audits = _audits(api, run_id)
    resolution_audits = [
        row
        for row in audits
        if row.get("node_id") == "operation.resolve"
        and row.get("record_signature")
        and _nested(row, "execution_metadata", "operation_key") == operation_key
    ]
    _status, chain = api.request("POST", f"/v1/runs/{quote(run_id, safe='')}/verify-chain")
    if not isinstance(chain, dict):
        raise RuntimeError("audit verification response is malformed")
    proof = {
        "schema_version": 1,
        "checkpoint": "workflow3-ambiguous-no-reexecution-direct-live",
        "created_at": datetime.now(UTC).isoformat(),
        "health": health,
        "run_id": run_id,
        "approval": approval,
        "marker": {
            "baseline": baseline,
            "after_commit": baseline + 1,
            "final": _marker_count(),
            "operation_key": operation_key,
            "payload_hash": marker["payload_hash"],
            "receipt_sha256": hashlib.sha256(str(marker["receipt"]).encode()).hexdigest(),
            "created_at": marker["created_at"],
        },
        "operation": {
            "operation_key": operation_key,
            "after_timeout": after_timeout,
            "after_lookup": after_lookup,
            "after_refusal": after_refusal,
            "after_resolution": after_resolution,
        },
        "fault": fault,
        "attempts": _count_attempts(audits, int(after_refusal["reconciliation_attempts"])),
        "dispatch_refusal": {
            "first_public_status": current_run.get("status"),
            "second_http_status": second_refusal["http_status"],
            "final_public_status": final_run.get("status"),
        },
        "runs": {
            "after_lookup": _safe_run(current_run),
            "after_refusal": _safe_run(second_refusal["run"]),
            "after_resolution": _safe_run(final_run),
        },
        "resolution": {
            "http_status": resolution_status,
            "state": resolution.get("state"),
            "reason_sha256": hashlib.sha256(reason.encode()).hexdigest(),
            "signed_audit_count": len(resolution_audits),
        },
        "chain": {
            field: chain.get(field)
            for field in (
                "verified",
                "signature_verified",
                "record_count",
                "unsigned_record_count",
            )
        },
        "audits": audits,
        "commands": commands,
        "discrepancies": [
            {
                "id": "missing-reconciliation-resume-surface",
                "status": "open",
                "observation": (
                    "The operation is COMPLETED, but the enclosing run remains "
                    "waiting_interrupt because no explicit reconciliation-resume API/UI exists."
                ),
            }
        ],
        "ui": {"claimed": False, "reason": "not inspected yet"},
    }
    validate_proof(proof)
    return proof


def seal_checkpoint(
    proof: Mapping[str, Any],
    *,
    destination: Path,
    screenshot: Path | None = None,
) -> Path:
    validate_proof(proof)
    if destination.exists():
        raise RuntimeError(f"checkpoint already exists: {destination}")
    store = EvidenceStore(destination)
    safe_proof = deepcopy(dict(proof))
    commands = safe_proof.pop("commands", [])
    if not isinstance(commands, list):
        raise RuntimeError("checkpoint command evidence is malformed")
    paths: dict[str, str] = {}
    for index, command in enumerate(commands, start=1):
        if not isinstance(command, dict):
            raise RuntimeError("checkpoint command record is malformed")
        store.record_command(
            sequence=index,
            name="focused-tests" if index == 1 else "backend-recreate",
            argv=command["argv"],
            working_directory=Path(command["working_directory"]),
            exit_code=int(command["exit_code"]),
            stdout=str(command["stdout"]),
            stderr=str(command["stderr"]),
        )
    ui = safe_proof.get("ui")
    if screenshot is not None:
        store.ingest_artifact(screenshot, "screenshots/operation-resolved.png")
        safe_proof["ui"] = {
            "claimed": True,
            "route": f"/console/runs/?run={proof['run_id']}",
            "screenshot": "screenshots/operation-resolved.png",
        }
    elif isinstance(ui, dict):
        ui["claimed"] = False
    store._write_exclusive(Path("runtime/proof.json"), safe_proof)
    paths["proof"] = "runtime/proof.json"
    health_event = store.append_event(
        "runtime.health.observed",
        {"graph_version_ref": GRAPH, "result": "pass"},
    )
    lifecycle_event = store.append_event(
        "operation.ambiguous.lifecycle.verified",
        {
            "result": "pass",
            "proof": paths["proof"],
            "marker_delta": 1,
            "lookup_attempts": 1,
            "automatic_reexecution_count": 0,
            "resolution_state": "COMPLETED",
        },
        correlation=CorrelationIds(
            run_id=str(proof["run_id"]),
            operation_id=str(_nested(proof, "operation", "operation_key")),
        ),
    )
    criteria = [
        AcceptanceCriterion(
            "workflow3.ambiguous-no-reexecution",
            "pass",
            (paths["proof"], f"events.ndjson#{lifecycle_event}"),
        )
    ]
    store.write_manifest(
        {
            "schema_version": 1,
            "campaign_id": TENANT,
            "checkpoint": "workflow3-ambiguous-no-reexecution-direct-live",
            "created_at": proof["created_at"],
            "run_id": proof["run_id"],
            "deployment_ref": DEPLOYMENT,
            "deployment_version": DEPLOYMENT_VERSION,
            "graph_version_ref": GRAPH,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "health_event": health_event,
        }
    )
    store.finalize_bundle(
        acceptance=criteria,
        report_markdown=(
            "# Workflow 3 ambiguous-outcome direct live checkpoint\n\n"
            "One fresh approved Workflow 3 v4 run committed exactly one local action marker and "
            "then timed out. The campaign-only authoritative lookup outage was consumed exactly "
            "once. The first replay durably paused for reconciliation and a repeated replay was "
            "refused, leaving the operation AMBIGUOUS with one reconciliation attempt and zero "
            "action reexecution. The authorized operator API "
            "then resolved the operation COMPLETED from the authoritative local receipt; its "
            "audit record is signed and the final chain verifies. The run remains safely paused "
            "rather than automatically executing after out-of-band reconciliation.\n\n"
            + (
                "The existing Runs evidence surface visibly exposed the resolved operation state; "
                "a sanitized Playwright screenshot is included.\n"
                if screenshot is not None
                else "No UI claim is made by this checkpoint.\n"
            )
        ),
    )
    return destination


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    collect = sub.add_parser("collect")
    collect.add_argument("--output", type=Path, required=True)
    resume = sub.add_parser("complete-paused")
    resume.add_argument("--run-id", required=True)
    resume.add_argument("--output", type=Path, required=True)
    seal = sub.add_parser("seal")
    seal.add_argument("--proof", type=Path, required=True)
    seal.add_argument("--destination", type=Path, required=True)
    seal.add_argument("--screenshot", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "collect":
        proof = collect_live()
        args.output.write_text(json.dumps(proof, sort_keys=True, indent=2) + "\n")
        return 0
    if args.command == "complete-paused":
        proof = complete_paused_live(args.run_id)
        args.output.write_text(json.dumps(proof, sort_keys=True, indent=2) + "\n")
        return 0
    proof = json.loads(args.proof.read_text(encoding="utf-8"))
    seal_checkpoint(proof, destination=args.destination, screenshot=args.screenshot)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
