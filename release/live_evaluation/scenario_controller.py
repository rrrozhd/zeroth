"""Authenticated local-only controller for Playwright resilience scenarios.

The controller owns fixture identity, durable evidence and campaign-local fault
state.  It never discovers or mutates a Zeroth run heuristically: runtime
barriers and observations cross an injected loopback gateway and fail closed
when that boundary is absent.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict

from .action_sink import EvaluationActionSink
from .evidence import CorrelationIds, EvidenceStore, UnsafeEvidenceError
from .fault_control import EvaluationFaultState

_SAFE_TEXT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,191}$")
_RUN_STATUSES = {"completed", "failed", "cancelled", "paused"}
_SCENARIOS = {
    "w1_empty_query",
    "w1_oversized_query",
    "w1_no_result",
    "w1_conflicting_documents",
    "w1_bad_credential",
    "w1_provider_timeout",
    "w1_rate_limit",
    "w1_malformed_response",
    "w1_excessive_revision",
    "w2_empty_batch",
    "w2_over_24_batch",
    "w2_malformed_item",
    "w2_retrieval_miss",
    "w2_cancellation",
    "w2_refresh_restoration",
    "w2_child_pause_partial",
    "w2_child_failure_partial",
    "w3_rejection",
    "w3_refresh_before_approval",
    "w3_sla_expiry",
    "w3_duplicate_submission",
    "w3_cancellation_after_approval",
    "w3_restart_before_receipt",
    "w3_restart_after_receipt",
    "w3_sink_unavailable",
    "w3_timeout_after_commit",
    "w3_ambiguous_no_reexecution",
}
_PREPARE_FAULTS = {
    "w1_no_result": ("connector", "retrieval_miss", {}),
    "w1_bad_credential": ("provider", "invalid_secret_reference", {}),
    "w1_provider_timeout": ("provider", "timeout", {"after_ms": 10}),
    "w1_rate_limit": ("provider", "rate_limit", {"status": 429}),
    "w1_malformed_response": ("provider", "malformed_response", {}),
    "w2_retrieval_miss": ("connector", "retrieval_miss", {}),
    "w3_restart_after_receipt": ("runtime", "post_receipt_pre_checkpoint", {}),
}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class ScenarioPrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    workflow_id: str
    expected: dict[str, object]
    deterministic_provider_fault: bool = False


@dataclass(frozen=True, slots=True)
class ScenarioFixture:
    fixture_id: str
    scenario_id: str
    workflow_id: str
    expected: dict[str, object]
    input_payload: dict[str, object]
    operation_id: str
    ui_action_id: str
    marker_count_before: int
    prepared_evidence: str
    baseline_run_ids: tuple[str, ...] = ()


class ScenarioControllerRuntimeGateway:
    """Narrow seam for authoritative loopback-only runtime controls."""

    def snapshot_run_ids(self, workflow_id: str) -> tuple[str, ...]:
        del workflow_id
        return ()

    def checkpoint(self, fixture: ScenarioFixture, checkpoint: str) -> dict[str, object]:
        raise NotImplementedError

    def restart_status(self, fixture: ScenarioFixture) -> dict[str, object]:
        raise NotImplementedError

    def verify(self, fixture: ScenarioFixture) -> dict[str, object]:
        raise NotImplementedError

    def cleanup(self, fixture: ScenarioFixture) -> dict[str, object]:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class LoopbackDeployment:
    base_url: str
    deployment_ref: str


class LoopbackHttpScenarioRuntimeGateway(ScenarioControllerRuntimeGateway):
    """Bind browser runs through exact public-API set subtraction."""

    def __init__(
        self,
        *,
        campaign_id: str,
        deployments: dict[str, LoopbackDeployment],
        client: httpx.Client | None = None,
        headers: dict[str, str] | None = None,
        supervisor: object | None = None,
        receipt_barriers: object | None = None,
        request_timeout_seconds: float = 10.0,
        sla_poll_attempts: int = 30,
        sla_poll_interval_seconds: float = 0.5,
    ) -> None:
        if not deployments:
            raise ValueError("at least one loopback deployment is required")
        if not 0 < request_timeout_seconds <= 30:
            raise ValueError("runtime gateway timeout must be positive and bounded")
        if not 1 <= sla_poll_attempts <= 120 or not 0 <= sla_poll_interval_seconds <= 2:
            raise ValueError("SLA polling must be bounded")
        for workflow_id, deployment in deployments.items():
            if not _SAFE_TEXT_ID.fullmatch(workflow_id):
                raise ValueError("unsafe runtime gateway workflow identity")
            parsed = urlparse(deployment.base_url)
            if (
                parsed.scheme != "http"
                or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.port is None
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or parsed.path not in {"", "/"}
            ):
                raise ValueError("runtime gateway deployments must use explicit loopback origins")
            if not _SAFE_TEXT_ID.fullmatch(deployment.deployment_ref):
                raise ValueError("unsafe runtime gateway deployment identity")
        self.campaign_id = campaign_id
        self.deployments = dict(deployments)
        self.client = client or httpx.Client()
        self.headers = dict(headers or {})
        self.supervisor = supervisor
        self.receipt_barriers = receipt_barriers
        self.request_timeout_seconds = request_timeout_seconds
        self.sla_poll_attempts = sla_poll_attempts
        self.sla_poll_interval_seconds = sla_poll_interval_seconds

    def _deployment(self, workflow_id: str) -> LoopbackDeployment:
        try:
            return self.deployments[workflow_id]
        except KeyError as exc:
            raise RuntimeError("workflow has no configured loopback deployment") from exc

    def _request(
        self,
        method: str,
        deployment: LoopbackDeployment,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, object] | None = None,
        expected: set[int] | None = None,
    ) -> tuple[int, dict[str, object] | list[object]]:
        try:
            response = self.client.request(
                method,
                f"{deployment.base_url.rstrip('/')}{path}",
                params=params,
                json=json_body,
                headers=self.headers,
                timeout=self.request_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise RuntimeError("loopback runtime request failed") from exc
        if response.status_code not in (expected or {200}):
            raise RuntimeError(f"loopback runtime request returned status {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise RuntimeError("loopback runtime response was not JSON") from exc
        if not isinstance(body, (dict, list)):
            raise RuntimeError("loopback runtime response has an invalid shape")
        return response.status_code, body

    def _get(
        self,
        deployment: LoopbackDeployment,
        path: str,
        *,
        params: dict[str, str] | None = None,
    ) -> dict[str, object] | list[object]:
        _status_code, body = self._request("GET", deployment, path, params=params)
        return body

    def _list_runs(self, workflow_id: str) -> tuple[dict[str, object], ...]:
        deployment = self._deployment(workflow_id)
        body = self._get(deployment, "/admin/runs", params={"limit": "1000", "offset": "0"})
        if not isinstance(body, dict) or not isinstance(body.get("runs"), list):
            raise RuntimeError("run inventory response is malformed")
        result: list[dict[str, object]] = []
        for run in body["runs"]:
            if not isinstance(run, dict):
                raise RuntimeError("run inventory contains a malformed run")
            if run.get("deployment_ref") != deployment.deployment_ref:
                continue
            run_campaign = run.get("campaign_id")
            if run_campaign is not None and run_campaign != self.campaign_id:
                continue
            run_id = run.get("run_id")
            if not isinstance(run_id, str) or not _SAFE_TEXT_ID.fullmatch(run_id):
                raise RuntimeError("run inventory contains an unsafe run identity")
            result.append(run)
        return tuple(result)

    def snapshot_run_ids(self, workflow_id: str) -> tuple[str, ...]:
        return tuple(sorted(str(run["run_id"]) for run in self._list_runs(workflow_id)))

    def _bind(self, fixture: ScenarioFixture) -> tuple[LoopbackDeployment, str]:
        deployment = self._deployment(fixture.workflow_id)
        baseline = set(fixture.baseline_run_ids)
        candidates = [
            str(run["run_id"])
            for run in self._list_runs(fixture.workflow_id)
            if run["run_id"] not in baseline
        ]
        if len(candidates) != 1:
            raise RuntimeError(
                "scenario binding requires exactly one new deployment-scoped run; "
                f"observed {len(candidates)}"
            )
        return deployment, candidates[0]

    def checkpoint(self, fixture: ScenarioFixture, checkpoint: str) -> dict[str, object]:
        deployment, run_id = self._bind(fixture)
        if checkpoint in {"run_submitted", "approval_resolved"}:
            return {"state": "applied", "run_id": run_id}
        if checkpoint in {"refresh_before", "refresh_after"}:
            if fixture.scenario_id == "w2_refresh_restoration":
                run = self._get(deployment, f"/v1/runs/{run_id}")
                status = run.get("status") if isinstance(run, dict) else None
                if (
                    not isinstance(run, dict)
                    or run.get("run_id") != run_id
                    or run.get("deployment_ref") != deployment.deployment_ref
                    or not isinstance(status, str)
                    or not status
                ):
                    raise RuntimeError(
                        "workflow2 refresh proof requires the exact bound runtime run"
                    )
                return {
                    "state": "observed",
                    "run_id": run_id,
                    "run_status": status,
                }
            evidence = self._get(deployment, f"/v1/runs/{run_id}/evidence")
            approvals = evidence.get("approvals") if isinstance(evidence, dict) else None
            pending = [
                approval
                for approval in approvals or []
                if isinstance(approval, dict)
                and approval.get("run_id") == run_id
                and approval.get("deployment_ref") == deployment.deployment_ref
                and approval.get("status") == "pending"
                and isinstance(approval.get("approval_id"), str)
            ]
            if len(pending) != 1:
                raise RuntimeError("refresh proof requires exactly one pending approval")
            return {
                "state": "observed",
                "run_id": run_id,
                "approval_id": pending[0]["approval_id"],
                "approval_state": "pending",
            }
        if checkpoint in {"restart_before_receipt_ready", "restart_after_receipt_ready"}:
            if self.supervisor is None:
                raise NotImplementedError("restart requires an owned deployment supervisor")
            restart = getattr(self.supervisor, "restart", None)
            if not callable(restart):
                raise NotImplementedError("restart supervisor has no restart boundary")
            barrier: dict[str, object] | None = None
            if checkpoint == "restart_after_receipt_ready":
                if self.receipt_barriers is None:
                    raise NotImplementedError(
                        "restart after receipt requires a durable receipt barrier"
                    )
                wait_for = getattr(self.receipt_barriers, "wait_for", None)
                if not callable(wait_for):
                    raise NotImplementedError("receipt barrier has no wait boundary")
                observed = wait_for(
                    campaign_id=self.campaign_id,
                    run_id=run_id,
                    timeout_seconds=10,
                )
                if (
                    not isinstance(observed, dict)
                    or observed.get("run_id") != run_id
                    or observed.get("state") != "waiting"
                    or not isinstance(observed.get("operation_key"), str)
                    or not isinstance(observed.get("audit_id"), str)
                    or not isinstance(observed.get("audit_digest"), str)
                    or not isinstance(observed.get("audit_signature_sha256"), str)
                ):
                    raise RuntimeError("receipt barrier identity is incomplete or mismatched")
                audits = self._get(
                    deployment,
                    f"/v1/deployments/{deployment.deployment_ref}/audits",
                )
                records = audits.get("records") if isinstance(audits, dict) else None
                matching = [
                    record
                    for record in records or []
                    if isinstance(record, dict)
                    and record.get("audit_id") == observed["audit_id"]
                    and record.get("run_id") == run_id
                ]
                if len(matching) != 1:
                    raise RuntimeError("receipt barrier audit is not publicly observable")
                signed = matching[0]
                signature = signed.get("record_signature")
                if (
                    signed.get("record_digest") != observed["audit_digest"]
                    or not isinstance(signature, str)
                    or not hmac.compare_digest(
                        hashlib.sha256(signature.encode()).hexdigest(),
                        str(observed["audit_signature_sha256"]),
                    )
                ):
                    raise RuntimeError("receipt barrier audit signature proof disagrees")
                verification = self._get(
                    deployment,
                    f"/v1/runs/{run_id}/audit-verification",
                )
                if (
                    not isinstance(verification, dict)
                    or verification.get("verified") is not True
                    or verification.get("signature_verified") is not True
                ):
                    raise RuntimeError("receipt barrier audit chain is not verified")
                barrier = observed
            restart(
                deployment_ref=deployment.deployment_ref,
                service_url=deployment.base_url,
            )
            if barrier is not None:
                mark_restarted = getattr(self.receipt_barriers, "mark_restarted", None)
                if callable(mark_restarted):
                    mark_restarted(campaign_id=self.campaign_id, run_id=run_id)
                return {
                    "state": "restart_requested",
                    "run_id": run_id,
                    "operation_key": barrier["operation_key"],
                    "audit_id": barrier["audit_id"],
                }
            return {"state": "restart_requested", "run_id": run_id}
        if checkpoint == "advance_sla":
            for _ in range(self.sla_poll_attempts):
                evidence = self._get(deployment, f"/v1/runs/{run_id}/evidence")
                approvals = evidence.get("approvals") if isinstance(evidence, dict) else None
                matching = [
                    approval
                    for approval in approvals or []
                    if isinstance(approval, dict)
                    and approval.get("run_id") == run_id
                    and approval.get("deployment_ref") == deployment.deployment_ref
                    and approval.get("sla_deadline") is not None
                ]
                if len(matching) != 1:
                    raise RuntimeError("SLA proof requires exactly one deadline-bearing approval")
                approval = matching[0]
                resolution = approval.get("resolution")
                actor = resolution.get("actor") if isinstance(resolution, dict) else None
                if (
                    approval.get("status") == "resolved"
                    and isinstance(resolution, dict)
                    and resolution.get("decision") == "reject"
                    and isinstance(actor, dict)
                    and actor.get("subject") == "sla_enforcer"
                ):
                    approval_id = approval.get("approval_id")
                    if not isinstance(approval_id, str) or not approval_id:
                        raise RuntimeError("SLA-resolved approval lacks identity")
                    status_code, cancelled = self._request(
                        "POST",
                        deployment,
                        f"/admin/runs/{run_id}/cancel",
                        expected={200},
                    )
                    if (
                        status_code != 200
                        or not isinstance(cancelled, dict)
                        or cancelled.get("run_id") != run_id
                        or cancelled.get("status") != "failed"
                    ):
                        raise RuntimeError("SLA-rejected run was not durably fenced")
                    return {
                        "state": "sla_expired",
                        "run_id": run_id,
                        "approval_id": approval_id,
                        "status_code": status_code,
                    }
                if self.sla_poll_interval_seconds:
                    time.sleep(self.sla_poll_interval_seconds)
            raise RuntimeError(
                "SLA checker did not produce an authoritative rejection before timeout"
            )
        if checkpoint == "duplicate_submission":
            evidence = self._get(deployment, f"/v1/runs/{run_id}/evidence")
            approvals = evidence.get("approvals") if isinstance(evidence, dict) else None
            resolved = [
                approval
                for approval in approvals or []
                if isinstance(approval, dict)
                and approval.get("run_id") == run_id
                and approval.get("deployment_ref") == deployment.deployment_ref
                and approval.get("status") == "resolved"
                and isinstance(approval.get("resolution"), dict)
                and approval["resolution"].get("decision") == "approve"
            ]
            if len(resolved) != 1 or not isinstance(resolved[0].get("approval_id"), str):
                raise RuntimeError(
                    "duplicate approval proof requires exactly one resolved approval"
                )
            approval_id = str(resolved[0]["approval_id"])
            status_code, _body = self._request(
                "POST",
                deployment,
                (f"/v1/deployments/{deployment.deployment_ref}/approvals/{approval_id}/resolve"),
                json_body={"decision": "approve", "edited_payload": None},
                expected={409},
            )
            return {
                "state": "duplicate_refused",
                "run_id": run_id,
                "approval_id": approval_id,
                "status_code": status_code,
            }
        raise NotImplementedError(f"unsupported public runtime checkpoint: {checkpoint}")

    def restart_status(self, fixture: ScenarioFixture) -> dict[str, object]:
        deployment, run_id = self._bind(fixture)
        body = self._get(deployment, "/health")
        if not isinstance(body, dict) or body.get("deployment_ref") != deployment.deployment_ref:
            raise RuntimeError("restarted deployment health identity does not match")
        return {"state": "ready", "run_id": run_id}

    @staticmethod
    def _public_status(run: dict[str, object]) -> str:
        status_value = run.get("status")
        failure = run.get("failure_state")
        if (
            status_value == "failed"
            and isinstance(failure, dict)
            and failure.get("reason") == "operator_cancelled"
        ):
            return "cancelled"
        return {
            "succeeded": "completed",
            "paused_for_approval": "paused",
        }.get(str(status_value), str(status_value))

    def verify(self, fixture: ScenarioFixture) -> dict[str, object]:
        deployment, run_id = self._bind(fixture)
        run = self._get(deployment, f"/v1/runs/{run_id}")
        if (
            not isinstance(run, dict)
            or run.get("run_id") != run_id
            or run.get("deployment_ref") != deployment.deployment_ref
        ):
            raise RuntimeError("run lookup returned the wrong runtime identity")
        audits = self._get(
            deployment,
            f"/v1/deployments/{deployment.deployment_ref}/audits",
            params={"run_id": run_id},
        )
        if not isinstance(audits, dict) or not isinstance(audits.get("records"), list):
            raise RuntimeError("run audit response is malformed")
        records = audits["records"]
        if not records or not all(isinstance(record, dict) for record in records):
            raise RuntimeError("run has no authoritative audit records")
        verification = self._get(deployment, f"/v1/runs/{run_id}/audit-verification")
        if (
            not isinstance(verification, dict)
            or verification.get("verified") is not True
            or verification.get("signature_verified") is not True
        ):
            raise RuntimeError("run audit chain is not signed and verified")
        cost = self._get(deployment, f"/v1/deployments/{deployment.deployment_ref}/cost")
        if not isinstance(cost, dict) or not isinstance(cost.get("total_cost_usd"), (int, float)):
            raise RuntimeError("deployment cost observation is malformed")
        last = records[-1]
        audit_id = last.get("audit_id")
        if not isinstance(audit_id, str) or not audit_id:
            raise RuntimeError("run audit response lacks exact audit identity")
        cost_event_ids = [
            record.get("cost_event_id")
            for record in records
            if isinstance(record.get("cost_event_id"), str)
        ]
        action_metadata = [
            record.get("execution_metadata")
            for record in records
            if isinstance(record.get("execution_metadata"), dict)
            and record["execution_metadata"].get("manifest_ref")
            == "evaluation://synthetic-action/v1"
        ]
        first_executions = sum(
            metadata.get("operation_first_execution") is True for metadata in action_metadata
        )
        operation_states = [
            metadata.get("operation_state")
            for metadata in action_metadata
            if metadata.get("operation_state") in {"completed", "failed", "ambiguous"}
        ]
        result: dict[str, object] = {
            "run_status": self._public_status(run),
            "marker_count": 0,
            "reexecution_count": max(0, first_executions - 1),
            "run_id": run_id,
            "audit_event_id": audit_id,
            "deployment_total_cost_usd": float(cost["total_cost_usd"]),
        }
        if cost_event_ids:
            result["cost_event_id"] = cost_event_ids[-1]
        if operation_states:
            result["operation_status"] = operation_states[-1]
        terminal = run.get("terminal_output")
        if isinstance(terminal, dict) and isinstance(terminal.get("results"), list):
            result["partial_collection_count"] = sum(
                isinstance(item, dict) and item.get("error") is None for item in terminal["results"]
            )
        elif fixture.scenario_id == "w2_child_pause_partial":
            completed_subgraph_indices = {
                metadata.get("branch_index")
                for record in records
                if record.get("status") == "completed"
                and isinstance((metadata := record.get("execution_metadata")), dict)
                and isinstance(metadata.get("branch_index"), int)
                and isinstance(metadata.get("subgraph_run_id"), str)
            }
            result["partial_collection_count"] = len(completed_subgraph_indices)
        return result

    def cleanup(self, fixture: ScenarioFixture) -> dict[str, object]:
        del fixture
        return {"state": "cleaned"}


class _FixtureStore:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve(strict=False)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS scenario_fixtures (
                    fixture_id TEXT PRIMARY KEY,
                    scenario_id TEXT NOT NULL,
                    workflow_id TEXT NOT NULL,
                    expected_json TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    operation_id TEXT NOT NULL UNIQUE,
                    ui_action_id TEXT NOT NULL UNIQUE,
                    marker_count_before INTEGER NOT NULL,
                    prepared_evidence TEXT NOT NULL,
                    baseline_run_ids_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    cleaned_at TEXT
                )
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(scenario_fixtures)")
            }
            if "baseline_run_ids_json" not in columns:
                connection.execute(
                    """
                    ALTER TABLE scenario_fixtures
                    ADD COLUMN baseline_run_ids_json TEXT NOT NULL DEFAULT '[]'
                    """
                )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def insert(
        self,
        *,
        fixture_id: str,
        scenario_id: str,
        workflow_id: str,
        expected: dict[str, object],
        input_payload: dict[str, object],
        operation_id: str,
        ui_action_id: str,
        marker_count_before: int,
        prepared_evidence: str,
        baseline_run_ids: tuple[str, ...],
    ) -> ScenarioFixture:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO scenario_fixtures (
                    fixture_id, scenario_id, workflow_id, expected_json, input_json,
                    operation_id, ui_action_id, marker_count_before,
                    prepared_evidence, baseline_run_ids_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fixture_id,
                    scenario_id,
                    workflow_id,
                    json.dumps(expected, sort_keys=True, separators=(",", ":")),
                    json.dumps(input_payload, sort_keys=True, separators=(",", ":")),
                    operation_id,
                    ui_action_id,
                    marker_count_before,
                    prepared_evidence,
                    json.dumps(baseline_run_ids, separators=(",", ":")),
                    _utc_now(),
                ),
            )
        return self.get(fixture_id)

    def get(self, fixture_id: str) -> ScenarioFixture:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM scenario_fixtures WHERE fixture_id = ? AND cleaned_at IS NULL",
                (fixture_id,),
            ).fetchone()
        if row is None:
            raise KeyError(fixture_id)
        return ScenarioFixture(
            fixture_id=row["fixture_id"],
            scenario_id=row["scenario_id"],
            workflow_id=row["workflow_id"],
            expected=json.loads(row["expected_json"]),
            input_payload=json.loads(row["input_json"]),
            operation_id=row["operation_id"],
            ui_action_id=row["ui_action_id"],
            marker_count_before=int(row["marker_count_before"]),
            prepared_evidence=row["prepared_evidence"],
            baseline_run_ids=tuple(json.loads(row["baseline_run_ids_json"])),
        )

    def mark_cleaned(self, fixture_id: str) -> None:
        with self._connect() as connection:
            changed = connection.execute(
                """
                UPDATE scenario_fixtures SET cleaned_at = ?
                WHERE fixture_id = ? AND cleaned_at IS NULL
                """,
                (_utc_now(), fixture_id),
            )
        if changed.rowcount != 1:
            raise KeyError(fixture_id)


def _validate_prepare(payload: ScenarioPrepareRequest) -> None:
    if payload.scenario_id not in _SCENARIOS:
        raise HTTPException(status_code=422, detail="unsupported scenario_id")
    if not _SAFE_TEXT_ID.fullmatch(payload.workflow_id):
        raise HTTPException(status_code=422, detail="unsafe workflow_id")
    prefix = payload.scenario_id[:2]
    if prefix not in {"w1", "w2", "w3"}:
        raise HTTPException(status_code=422, detail="scenario workflow is invalid")
    expected = payload.expected
    if expected.get("run_status") not in _RUN_STATUSES:
        raise HTTPException(status_code=422, detail="invalid expected run_status")
    for field in ("marker_count", "reexecution_count"):
        value = expected.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise HTTPException(status_code=422, detail=f"invalid expected {field}")
    allowed = {
        "run_status",
        "marker_count",
        "reexecution_count",
        "partial_collection_count",
        "operation_status",
    }
    if set(expected) - allowed:
        raise HTTPException(status_code=422, detail="unsupported expected field")


def _input_payload(scenario_id: str, fixture_id: str) -> dict[str, object]:
    suffix = scenario_id[3:]
    if scenario_id.startswith("w1_"):
        if suffix == "empty_query":
            return {"query": ""}
        if suffix == "oversized_query":
            return {"query": "x" * 65_537}
        query = {
            "no_result": "synthetic-no-result",
            "conflicting_documents": "synthetic-conflict",
            "excessive_revision": "synthetic-excessive-revision",
        }.get(suffix, "known-answer-1")
        return {"query": query}
    if scenario_id.startswith("w2_"):
        if suffix == "empty_batch":
            return {"items": []}
        if suffix == "over_24_batch":
            return {
                "items": [
                    {"index": index, "query": f"investigation-over-limit-{index}"}
                    for index in range(25)
                ]
            }
        if suffix == "malformed_item":
            return {"items": [{"index": 0}]}
        result = {
            "items": [
                {
                    "index": index,
                    "query": (
                        f"investigation-negative-{suffix.replace('_', '-')}"
                        f"-{fixture_id[:8]}-{index}"
                    ),
                }
                for index in range(8)
            ]
        }
        if suffix in {"child_pause_partial", "child_failure_partial"}:
            result["items"][3]["evaluation_behavior"] = (
                "child_pause" if suffix == "child_pause_partial" else "child_failure"
            )
        return result
    ticket_suffix = suffix.replace("_", "-")[:52]
    result: dict[str, object] = {
        "ticket": f"synthetic-{ticket_suffix}-{fixture_id[:8]}",
        "status": "remediated",
    }
    if suffix == "sink_unavailable":
        result["fault"] = "unavailable"
    elif suffix in {"timeout_after_commit", "ambiguous_no_reexecution"}:
        result["fault"] = "timeout_after_commit"
    return result


def create_scenario_controller_app(
    *,
    campaign_id: str,
    artifact_root: Path,
    evidence_store: EvidenceStore,
    fault_state: EvaluationFaultState,
    action_sink: EvaluationActionSink,
    controller_key: str,
    runtime_gateway: ScenarioControllerRuntimeGateway | None = None,
) -> FastAPI:
    """Build the controller without starting processes or making network calls."""
    resolved_root = artifact_root.expanduser().resolve(strict=False)
    if evidence_store.root != resolved_root and not evidence_store.root.is_relative_to(
        resolved_root
    ):
        raise ValueError("evidence store must be campaign-scoped under artifact root")
    if action_sink.root != resolved_root and not action_sink.root.is_relative_to(resolved_root):
        raise ValueError("action sink must be campaign-scoped under artifact root")
    if not controller_key:
        raise ValueError("controller key is required")
    fixtures = _FixtureStore(resolved_root / "scenario-controller.sqlite3")
    app = FastAPI(title="Zeroth local evaluation scenario controller")

    def authorize(x_controller_key: str | None) -> None:
        if x_controller_key is None or not hmac.compare_digest(x_controller_key, controller_key):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

    def fixture_or_404(fixture_id: str) -> ScenarioFixture:
        if not _SAFE_TEXT_ID.fullmatch(fixture_id):
            raise HTTPException(status_code=404, detail="scenario fixture not found")
        try:
            return fixtures.get(fixture_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="scenario fixture not found") from exc

    def correlation(
        fixture: ScenarioFixture, observation: dict[str, object] | None = None
    ) -> CorrelationIds:
        observation = observation or {}
        values = {
            "operation_id": fixture.operation_id,
            "ui_action_id": fixture.ui_action_id,
            "run_id": observation.get("run_id"),
            "audit_event_id": observation.get("audit_event_id"),
            "cost_event_id": observation.get("cost_event_id"),
        }
        return CorrelationIds(
            **{key: value for key, value in values.items() if isinstance(value, str)}
        )

    def blocked(fixture: ScenarioFixture, detail: str) -> HTTPException:
        event_id = evidence_store.append_event(
            "scenario.blocked",
            {"fixture_id": fixture.fixture_id, "reason": detail},
            correlation=correlation(fixture),
        )
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=detail,
            headers={"X-Evidence-Ref": f"events.ndjson#{event_id}"},
        )

    @app.get("/health")
    def health(x_controller_key: str | None = Header(default=None)) -> dict[str, str]:
        authorize(x_controller_key)
        return {"campaign_id": campaign_id, "state": "ready"}

    @app.post("/v1/scenarios/prepare")
    def prepare(
        payload: ScenarioPrepareRequest,
        x_controller_key: str | None = Header(default=None),
    ) -> dict[str, object]:
        authorize(x_controller_key)
        _validate_prepare(payload)
        try:
            evidence_store.validate(payload.model_dump())
        except UnsafeEvidenceError as exc:
            raise HTTPException(status_code=422, detail="unsafe scenario request") from exc
        fixture_id = f"fixture-{uuid4().hex}"
        operation_id = f"scenario-operation-{uuid4().hex}"
        ui_action_id = f"scenario-ui-{uuid4().hex}"
        input_payload = _input_payload(payload.scenario_id, fixture_id)
        baseline_run_ids = (
            runtime_gateway.snapshot_run_ids(payload.workflow_id)
            if runtime_gateway is not None
            else ()
        )
        fault = _PREPARE_FAULTS.get(payload.scenario_id)
        fault_id: str | None = None
        if fault is not None:
            target, mode, parameters = fault
            armed = fault_state.arm(
                campaign_id=campaign_id,
                target=target,
                mode=mode,
                parameters=parameters,
            )
            fault_id = armed.fault_id
        event_id = evidence_store.append_event(
            "scenario.prepared",
            {
                "fixture_id": fixture_id,
                "scenario_id": payload.scenario_id,
                "workflow_id": payload.workflow_id,
                "fault_id": fault_id,
                "input_sha256": hashlib.sha256(
                    json.dumps(input_payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            },
            correlation=CorrelationIds(operation_id=operation_id, ui_action_id=ui_action_id),
        )
        reference = f"events.ndjson#{event_id}"
        fixture = fixtures.insert(
            fixture_id=fixture_id,
            scenario_id=payload.scenario_id,
            workflow_id=payload.workflow_id,
            expected=dict(payload.expected),
            input_payload=input_payload,
            operation_id=operation_id,
            ui_action_id=ui_action_id,
            marker_count_before=action_sink.marker_count(),
            prepared_evidence=reference,
            baseline_run_ids=baseline_run_ids,
        )
        result: dict[str, object] = {
            "fixture_id": fixture.fixture_id,
            "input_payload": fixture.input_payload,
            "correlation": correlation(fixture).as_dict(),
            "evidence": [reference],
        }
        if payload.scenario_id.startswith("w3_"):
            result["approval_node_id"] = "approval"
        return result

    @app.post("/v1/scenarios/{fixture_id}/checkpoints/{checkpoint}")
    def checkpoint(
        fixture_id: str,
        checkpoint: str,
        x_controller_key: str | None = Header(default=None),
    ) -> dict[str, object]:
        authorize(x_controller_key)
        fixture = fixture_or_404(fixture_id)
        if not _SAFE_TEXT_ID.fullmatch(checkpoint):
            raise HTTPException(status_code=422, detail="unsafe checkpoint")
        if runtime_gateway is None:
            raise blocked(fixture, "runtime gateway is not configured; checkpoint is blocked")
        try:
            result = runtime_gateway.checkpoint(fixture, checkpoint)
            evidence_store.validate(result)
        except (NotImplementedError, ValueError, RuntimeError) as exc:
            raise blocked(fixture, f"checkpoint is blocked: {type(exc).__name__}") from exc
        event_id = evidence_store.append_event(
            "scenario.checkpointed",
            {
                "fixture_id": fixture_id,
                "checkpoint": checkpoint,
                "state": str(result.get("state", "unknown")),
            },
            correlation=correlation(fixture, result),
        )
        return {**result, "evidence": f"events.ndjson#{event_id}"}

    @app.get("/v1/scenarios/{fixture_id}/restart-status")
    def restart_status(
        fixture_id: str,
        x_controller_key: str | None = Header(default=None),
    ) -> dict[str, object]:
        authorize(x_controller_key)
        fixture = fixture_or_404(fixture_id)
        if runtime_gateway is None:
            raise blocked(fixture, "runtime gateway is not configured; restart is blocked")
        try:
            result = runtime_gateway.restart_status(fixture)
            evidence_store.validate(result)
        except (NotImplementedError, ValueError, RuntimeError) as exc:
            raise blocked(fixture, f"restart is blocked: {type(exc).__name__}") from exc
        event_id = evidence_store.append_event(
            "scenario.restart-status.observed",
            {"fixture_id": fixture_id, "state": str(result.get("state", "unknown"))},
            correlation=correlation(fixture, result),
        )
        return {**result, "evidence": f"events.ndjson#{event_id}"}

    @app.get("/v1/scenarios/{fixture_id}/verify")
    def verify(
        fixture_id: str,
        x_controller_key: str | None = Header(default=None),
    ) -> dict[str, object]:
        authorize(x_controller_key)
        fixture = fixture_or_404(fixture_id)
        if runtime_gateway is None:
            raise blocked(fixture, "runtime gateway is not configured; verification is blocked")
        try:
            result = runtime_gateway.verify(fixture)
            evidence_store.validate(result)
        except (NotImplementedError, ValueError, RuntimeError) as exc:
            raise blocked(fixture, f"verification is blocked: {type(exc).__name__}") from exc
        if not all(
            isinstance(result.get(field), str) and result[field]
            for field in ("run_id", "audit_event_id")
        ):
            raise blocked(fixture, "verification lacks exact runtime correlation IDs")
        observed = dict(result)
        if fixture.scenario_id.startswith("w3_"):
            observed["marker_count"] = action_sink.marker_count() - fixture.marker_count_before
        event_id = evidence_store.append_event(
            "scenario.verified",
            {
                "fixture_id": fixture_id,
                "run_status": str(observed.get("run_status", "unknown")),
                "marker_count": observed.get("marker_count"),
                "reexecution_count": observed.get("reexecution_count"),
            },
            correlation=correlation(fixture, observed),
        )
        return {
            **observed,
            "correlation": correlation(fixture, observed).as_dict(),
            "evidence": [fixture.prepared_evidence, f"events.ndjson#{event_id}"],
        }

    @app.post("/v1/scenarios/{fixture_id}/cleanup")
    def cleanup(
        fixture_id: str,
        x_controller_key: str | None = Header(default=None),
    ) -> dict[str, object]:
        authorize(x_controller_key)
        fixture = fixture_or_404(fixture_id)
        result: dict[str, object] = {"state": "cleaned"}
        configured_fault = _PREPARE_FAULTS.get(fixture.scenario_id)
        if configured_fault is not None:
            target, _mode, _parameters = configured_fault
            disarmed = fault_state.consume(campaign_id=campaign_id, target=target)
            if disarmed is not None:
                result["unconsumed_fault_disarmed"] = True
        if runtime_gateway is not None:
            try:
                result = runtime_gateway.cleanup(fixture)
                evidence_store.validate(result)
            except (NotImplementedError, ValueError, RuntimeError) as exc:
                raise blocked(fixture, f"cleanup is blocked: {type(exc).__name__}") from exc
        event_id = evidence_store.append_event(
            "scenario.cleaned",
            {"fixture_id": fixture_id, "state": str(result.get("state", "unknown"))},
            correlation=correlation(fixture, result),
        )
        fixtures.mark_cleaned(fixture_id)
        return {**result, "evidence": f"events.ndjson#{event_id}"}

    return app
