"""Fail-closed controls and acceptance for governed-remediation negatives.

The controller deliberately does not know how to restart or cancel Zeroth.  A
campaign entrypoint must inject an explicitly scoped local runtime-control
boundary.  That makes an unimplemented scenario a hard blocker instead of a
test that silently exercises the happy path.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol

import httpx

from .action_runner import EVALUATION_ACTION_MANIFEST_SHA256
from .action_sink import EvaluationActionSink
from .browser_refresh import BoundedRefreshEvidenceProducer, BrowserRefreshEvidence
from .campaign_execution import WorkflowAction
from .campaign_http import BackendObservation
from .coordinator import ActionRecorder, CriterionResult, StepResult

OperationState = Literal["completed", "failed", "ambiguous"]


class UnsupportedWorkflow3ScenarioError(RuntimeError):
    """The requested resilience case has no safe local control implementation."""


class Workflow3RuntimeControls(Protocol):
    """Narrow, injected boundary for campaign-owned runtime mutations."""

    def invoke(self, *, scenario: str, checkpoint: str) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True)
class PreparedWorkflow3Scenario:
    marker_count_before: int
    fixture_id: str | None = None
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Workflow3RuntimeFacts:
    """Sanitized facts collected from HTTP, audit, and authoritative sink state."""

    scenario: str
    operation_key: str
    marker_count_before: int
    marker_count_after: int
    action_execution_count: int
    outcome_lookup_count: int
    automatic_reexecution_count: int
    approval_resolve_statuses: tuple[int, ...]
    audit_records: tuple[dict[str, object], ...]
    signed_chain_verified: bool
    run_status: str
    operation_state: OperationState
    approval_decisions: tuple[str, ...] = ()
    approval_id_before: str | None = None
    approval_id_after: str | None = None
    approval_state_before: str | None = None
    approval_state_after: str | None = None
    terminal_receipt: str | None = None
    authoritative_receipt: str | None = None
    terminal_payload_hash: str | None = None
    authoritative_payload_hash: str | None = None
    refreshed: bool = False
    restarted: bool = False
    cancelled: bool = False
    sla_expired: bool = False

    @property
    def marker_delta(self) -> int:
        return self.marker_count_after - self.marker_count_before


_CONTROL_CHECKPOINTS = {
    "negative-refresh-before-approval": "before_approval",
    "negative-sla-expiry": "while_approval_pending",
    "negative-cancellation-after-approval": "after_approval",
    "negative-restart-around-receipt": "after_receipt",
    "negative-ambiguous-no-reexecution": "after_action_attempt",
}


class Workflow3ScenarioController:
    """Campaign-local state guard around Workflow 3 resilience controls."""

    def __init__(
        self,
        *,
        sink: EvaluationActionSink,
        runtime_controls: Workflow3RuntimeControls | None = None,
    ) -> None:
        self.sink = sink
        self.runtime_controls = runtime_controls

    def prepare(self, action: WorkflowAction) -> PreparedWorkflow3Scenario:
        self._require_action(action)
        if action.scenario in _CONTROL_CHECKPOINTS and self.runtime_controls is None:
            raise UnsupportedWorkflow3ScenarioError(
                f"{action.scenario} requires an injected runtime control"
            )
        return PreparedWorkflow3Scenario(marker_count_before=self.sink.marker_count())

    def control(self, action: WorkflowAction, *, checkpoint: str) -> tuple[str, ...]:
        self._require_action(action)
        expected = _CONTROL_CHECKPOINTS.get(action.scenario)
        if expected is None:
            raise UnsupportedWorkflow3ScenarioError(
                f"{action.scenario} has no runtime control checkpoint"
            )
        if checkpoint != expected:
            raise UnsupportedWorkflow3ScenarioError(
                f"{action.scenario} requires checkpoint {expected!r}, not {checkpoint!r}"
            )
        if self.runtime_controls is None:
            raise UnsupportedWorkflow3ScenarioError(
                f"{action.scenario} requires an injected runtime control"
            )
        evidence = self.runtime_controls.invoke(
            scenario=action.scenario,
            checkpoint=checkpoint,
        )
        if not evidence or not all(isinstance(item, str) and item for item in evidence):
            raise RuntimeError("workflow3 runtime control did not persist durable evidence")
        return evidence

    @staticmethod
    def approval_decisions(action: WorkflowAction) -> tuple[str, ...]:
        """Return the only approval sequence allowed for the negative case."""
        Workflow3ScenarioController._require_action(action)
        if action.scenario in {
            "negative-rejection-zero-marker",
            "negative-refresh-before-approval",
        }:
            return ("reject",)
        if action.scenario == "negative-sla-expiry":
            return ()
        if action.scenario == "negative-duplicate-submission":
            return ("approve", "approve")
        return ("approve",)

    def finalize(
        self,
        action: WorkflowAction,
        *,
        prepared: PreparedWorkflow3Scenario,
        reported: Workflow3RuntimeFacts,
    ) -> Workflow3RuntimeFacts:
        """Replace untrusted marker/receipt claims with authoritative SQLite state."""
        self._require_action(action)
        if reported.scenario != action.scenario or not reported.operation_key:
            raise RuntimeError("workflow3 runtime facts do not match the prepared scenario")
        receipt = self.sink.lookup(reported.operation_key)
        return replace(
            reported,
            marker_count_before=prepared.marker_count_before,
            marker_count_after=self.sink.marker_count(),
            authoritative_receipt=None if receipt is None else receipt.receipt,
            authoritative_payload_hash=None if receipt is None else receipt.payload_hash,
        )

    @staticmethod
    def _require_action(action: WorkflowAction) -> None:
        if action.workflow != "workflow3" or action.action_type != "negative":
            raise UnsupportedWorkflow3ScenarioError(
                "workflow3 controller received an incompatible campaign action"
            )


class RemoteWorkflow3ScenarioController(Workflow3ScenarioController):
    """Drive W3 approval/checkpoint controls through the prestarted controller."""

    _SCENARIOS = {
        "negative-rejection-zero-marker": "w3_rejection",
        "negative-refresh-before-approval": "w3_refresh_before_approval",
        "negative-sla-expiry": "w3_sla_expiry",
        "negative-duplicate-submission": "w3_duplicate_submission",
        "negative-cancellation-after-approval": "w3_cancellation_after_approval",
        "negative-restart-around-receipt": "w3_restart_after_receipt",
        "negative-sink-unavailable": "w3_sink_unavailable",
        "negative-timeout-after-commit": "w3_timeout_after_commit",
        "negative-ambiguous-no-reexecution": "w3_ambiguous_no_reexecution",
    }

    def __init__(
        self,
        *,
        sink: EvaluationActionSink,
        controller_url: str,
        controller_key: str,
        workflow_id: str,
        client: httpx.Client,
        refresh_producer: BoundedRefreshEvidenceProducer | None = None,
    ) -> None:
        super().__init__(sink=sink)
        self.controller_url = controller_url.rstrip("/")
        self.controller_key = controller_key
        self.workflow_id = workflow_id
        self.client = client
        self.refresh_producer = refresh_producer
        self._prepared: dict[str, PreparedWorkflow3Scenario] = {}
        self._traces: dict[str, dict[str, object]] = {}

    def execute_refresh(
        self, action: WorkflowAction, recorder: ActionRecorder
    ) -> BrowserRefreshEvidence:
        if action.scenario != "negative-refresh-before-approval":
            raise UnsupportedWorkflow3ScenarioError("incompatible browser refresh scenario")
        if self.refresh_producer is None:
            raise UnsupportedWorkflow3ScenarioError(
                "refresh-before-approval requires the bounded Playwright producer"
            )
        result = self.refresh_producer.run("w3_refresh_before_approval", recorder=recorder)
        self._traces[action.request.identity.operation_id] = {
            "approval_decisions": ["reject"],
            "approval_resolve_statuses": [200],
            "refreshed": True,
            "approval_id_before": result.approval_id_before,
            "approval_id_after": result.approval_id_after,
            "approval_state_before": result.approval_state_before,
            "approval_state_after": result.approval_state_after,
        }
        return result

    def _remote(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = self.client.request(
            method,
            f"{self.controller_url}{path}",
            headers={"X-Controller-Key": self.controller_key},
            timeout=30.0,
            **kwargs,
        )
        if not 200 <= response.status_code < 300:
            raise RuntimeError("workflow3 remote scenario control failed")
        return response

    def prepare(self, action: WorkflowAction) -> PreparedWorkflow3Scenario:
        self._require_action(action)
        local = PreparedWorkflow3Scenario(marker_count_before=self.sink.marker_count())
        scenario_id = self._SCENARIOS.get(action.scenario)
        if scenario_id is None:
            raise UnsupportedWorkflow3ScenarioError(
                f"unsupported remote workflow3 scenario: {action.scenario}"
            )
        one_marker = action.scenario in {
            "negative-duplicate-submission",
            "negative-restart-around-receipt",
            "negative-timeout-after-commit",
            "negative-ambiguous-no-reexecution",
        }
        run_status = (
            "completed"
            if action.scenario
            in {
                "negative-duplicate-submission",
                "negative-restart-around-receipt",
                "negative-timeout-after-commit",
            }
            else "cancelled"
            if action.scenario == "negative-cancellation-after-approval"
            else "failed"
        )
        expected: dict[str, object] = {
            "run_status": run_status,
            "marker_count": int(one_marker),
            "reexecution_count": 0,
        }
        if action.scenario == "negative-ambiguous-no-reexecution":
            expected["operation_status"] = "ambiguous"
        elif one_marker:
            expected["operation_status"] = "completed"
        response = self._remote(
            "POST",
            "/v1/scenarios/prepare",
            json={
                "scenario_id": scenario_id,
                "workflow_id": self.workflow_id,
                "expected": expected,
            },
        )
        body = response.json()
        fixture_id = body.get("fixture_id") if isinstance(body, dict) else None
        evidence = body.get("evidence") if isinstance(body, dict) else None
        if (
            not isinstance(fixture_id, str)
            or not isinstance(evidence, list)
            or not all(isinstance(item, str) and item for item in evidence)
        ):
            raise RuntimeError("workflow3 remote preparation response is malformed")
        prepared = replace(local, fixture_id=fixture_id, evidence=tuple(evidence))
        self._prepared[action.request.identity.operation_id] = prepared
        self._traces[action.request.identity.operation_id] = {
            "approval_decisions": [],
            "approval_resolve_statuses": [],
        }
        return prepared

    def handle_paused(
        self,
        action: WorkflowAction,
        *,
        approval_id: str,
        run_id: str,
        base_url: str,
        request: Any,
        recorder: ActionRecorder,
    ) -> tuple[str, ...]:
        operation_id = action.request.identity.operation_id
        prepared = self._prepared[operation_id]
        if prepared.fixture_id is None:
            raise RuntimeError("workflow3 remote fixture identity is missing")
        if action.scenario == "negative-refresh-before-approval":
            raise UnsupportedWorkflow3ScenarioError(
                "refresh-before-approval requires the targeted browser producer"
            )
        trace = self._traces[operation_id]
        evidence: list[str] = list(prepared.evidence)

        def checkpoint(name: str) -> dict[str, object]:
            response = self._remote(
                "POST",
                f"/v1/scenarios/{prepared.fixture_id}/checkpoints/{name}",
            )
            body = response.json()
            if not isinstance(body, dict) or body.get("run_id") != run_id:
                raise RuntimeError("workflow3 checkpoint bound a different run")
            reference = body.get("evidence")
            if not isinstance(reference, str) or not reference:
                raise RuntimeError("workflow3 checkpoint omitted durable evidence")
            evidence.append(reference)
            return body

        def resolve(decision: str) -> None:
            response = request(
                "POST",
                (
                    f"{base_url}/v1/deployments/{action.request.deployment_ref}"
                    f"/approvals/{approval_id}/resolve"
                ),
                json={"decision": decision, "edited_payload": None},
                expected={200},
            )
            trace["approval_decisions"].append(decision)  # type: ignore[union-attr]
            trace["approval_resolve_statuses"].append(response.status_code)  # type: ignore[union-attr]
            evidence.append(
                recorder.record_api_result(
                    method="POST",
                    path=(
                        f"/v1/deployments/{action.request.deployment_ref}"
                        f"/approvals/{approval_id}/resolve"
                    ),
                    status_code=response.status_code,
                    metadata={"decision": decision},
                )
            )

        if action.scenario == "negative-rejection-zero-marker":
            resolve("reject")
        elif action.scenario == "negative-sla-expiry":
            checkpoint("advance_sla")
            trace["sla_expired"] = True
        elif action.scenario == "negative-cancellation-after-approval":
            decisions = trace["approval_decisions"]
            if not decisions:
                resolve("approve")
            else:
                checkpoint("approval_resolved")
                cancelled = request(
                    "POST",
                    f"{base_url}/admin/runs/{run_id}/cancel",
                    expected={200},
                )
                trace["cancelled"] = True
                evidence.append(
                    recorder.record_api_result(
                        method="POST",
                        path=f"/admin/runs/{run_id}/cancel",
                        status_code=cancelled.status_code,
                        metadata={"checkpoint": "evaluation_pre_action_barrier"},
                    )
                )
        else:
            resolve("approve")
            if action.scenario == "negative-duplicate-submission":
                duplicate = checkpoint("duplicate_submission")
                trace["approval_resolve_statuses"].append(  # type: ignore[union-attr]
                    int(duplicate["status_code"])
                )
            elif action.scenario == "negative-restart-around-receipt":
                checkpoint("restart_after_receipt_ready")
                trace["restarted"] = True
        return tuple(evidence)

    def trace(self, action: WorkflowAction) -> dict[str, object]:
        return dict(self._traces[action.request.identity.operation_id])

    def verify(self, action: WorkflowAction, *, run_id: str) -> tuple[str, ...]:
        prepared = self._prepared[action.request.identity.operation_id]
        if prepared.fixture_id is None:
            raise RuntimeError("workflow3 remote fixture identity is missing")
        response = self._remote("GET", f"/v1/scenarios/{prepared.fixture_id}/verify")
        body = response.json()
        references = body.get("evidence") if isinstance(body, dict) else None
        if (
            not isinstance(body, dict)
            or body.get("run_id") != run_id
            or not isinstance(references, list)
            or not all(isinstance(item, str) and item for item in references)
        ):
            raise RuntimeError("workflow3 remote verification is malformed or misbound")
        return tuple(references)


class Workflow3NegativeEvaluator:
    """Exact semantic gates for all nine Workflow 3 negative cases."""

    _ONE_MARKER = {
        "negative-duplicate-submission",
        "negative-restart-around-receipt",
        "negative-timeout-after-commit",
    }
    _ZERO_MARKER = {
        "negative-rejection-zero-marker",
        "negative-refresh-before-approval",
        "negative-sla-expiry",
        "negative-cancellation-after-approval",
        "negative-sink-unavailable",
    }

    @staticmethod
    def _facts(action: WorkflowAction, observation: BackendObservation) -> Workflow3RuntimeFacts:
        facts = observation.workflow3
        if not isinstance(facts, Workflow3RuntimeFacts) or facts.scenario != action.scenario:
            raise RuntimeError("workflow3 negative lacks matching runtime facts")
        records = (observation.audits or {}).get("records")
        if (
            not isinstance(records, list)
            or tuple(records) != facts.audit_records
            or (observation.audits or {}).get("chain_verified") is not True
        ):
            raise RuntimeError("workflow3 runtime facts disagree with observed signed audit data")
        return facts

    @staticmethod
    def _assert_signed_audit(facts: Workflow3RuntimeFacts) -> None:
        if not facts.signed_chain_verified or not facts.audit_records:
            raise RuntimeError("workflow3 negative lacks a verified signed audit chain")
        if any(not record.get("record_signature") for record in facts.audit_records):
            raise RuntimeError("workflow3 negative contains an unsigned audit record")
        action_records = [
            record
            for record in facts.audit_records
            if isinstance(record.get("execution_metadata"), dict)
            and record["execution_metadata"].get("manifest_ref_sha256")
            == EVALUATION_ACTION_MANIFEST_SHA256
        ]
        if len(action_records) != facts.action_execution_count:
            raise RuntimeError("workflow3 action attempts do not match signed audit records")
        if any(
            record["execution_metadata"].get("operation_key") != facts.operation_key
            for record in action_records
        ):
            raise RuntimeError("workflow3 action audit is not linked to its operation key")

    @staticmethod
    def _assert_receipt(facts: Workflow3RuntimeFacts) -> None:
        if not facts.terminal_receipt or not facts.authoritative_receipt:
            raise RuntimeError("workflow3 action receipt is missing")
        if facts.terminal_receipt != facts.authoritative_receipt:
            raise RuntimeError("workflow3 receipt disagrees with authoritative outcome lookup")
        if not facts.terminal_payload_hash or not facts.authoritative_payload_hash:
            raise RuntimeError("workflow3 action payload hash is missing")
        if facts.terminal_payload_hash != facts.authoritative_payload_hash:
            raise RuntimeError("workflow3 payload hash disagrees with authoritative sink state")

    def evaluate(self, action: WorkflowAction, observation: BackendObservation) -> StepResult:
        if action.workflow != "workflow3" or action.action_type != "negative":
            raise RuntimeError("workflow3 negative evaluator received an incompatible action")
        if not observation.evidence:
            raise RuntimeError("workflow3 negative has no durable evidence")
        facts = self._facts(action, observation)
        self._assert_signed_audit(facts)
        if facts.automatic_reexecution_count != 0:
            raise RuntimeError("automatic action reexecution detected")
        if facts.marker_delta < 0:
            raise RuntimeError("action marker count decreased during scenario")

        if action.scenario in self._ONE_MARKER:
            if facts.marker_delta != 1:
                raise RuntimeError("workflow3 scenario did not create exactly one marker")
            if facts.action_execution_count != 1:
                raise RuntimeError("workflow3 scenario did not execute the action exactly once")
            if facts.operation_state != "completed":
                raise RuntimeError("durable action operation is not completed")
            self._assert_receipt(facts)
        elif action.scenario == "negative-ambiguous-no-reexecution":
            if facts.marker_delta != 1 or facts.action_execution_count != 1:
                raise RuntimeError("ambiguous workflow3 action did not create exactly one marker")
            if facts.operation_state != "ambiguous":
                raise RuntimeError("unresolved workflow3 action was not retained as ambiguous")
            if facts.terminal_receipt is not None or not facts.authoritative_receipt:
                raise RuntimeError("ambiguous workflow3 receipt evidence is inconsistent")
            if facts.terminal_payload_hash is not None or not facts.authoritative_payload_hash:
                raise RuntimeError("ambiguous workflow3 payload-hash evidence is inconsistent")
        elif action.scenario in self._ZERO_MARKER:
            if facts.marker_delta != 0:
                raise RuntimeError("workflow3 zero-effect scenario created an action marker")
            if any(
                value is not None
                for value in (
                    facts.terminal_receipt,
                    facts.authoritative_receipt,
                    facts.terminal_payload_hash,
                    facts.authoritative_payload_hash,
                )
            ):
                raise RuntimeError("zero-effect workflow3 scenario reported an action receipt")
        else:
            raise RuntimeError(f"unsupported workflow3 negative scenario: {action.scenario}")

        self._assert_scenario(action.scenario, facts)
        return StepResult(
            tuple(
                CriterionResult(criterion_id, "pass", observation.evidence)
                for criterion_id in action.criterion_ids
            )
        )

    @staticmethod
    def _assert_scenario(scenario: str, facts: Workflow3RuntimeFacts) -> None:
        if scenario == "negative-rejection-zero-marker":
            if facts.approval_decisions != ("reject",) or facts.approval_resolve_statuses != (200,):
                raise RuntimeError("approval rejection was not resolved exactly once")
            if facts.action_execution_count != 0:
                raise RuntimeError("rejected approval attempted the action")
        elif scenario == "negative-refresh-before-approval":
            if (
                not facts.refreshed
                or facts.approval_state_before != "pending"
                or facts.approval_state_after != "pending"
                or not facts.approval_id_before
                or facts.approval_id_before != facts.approval_id_after
            ):
                raise RuntimeError("approval identity/state was not restored after refresh")
            if facts.approval_decisions != ("reject",) or facts.action_execution_count != 0:
                raise RuntimeError("refreshed approval was not rejected without action execution")
        elif scenario == "negative-sla-expiry":
            if (
                not facts.sla_expired
                or facts.approval_state_after != "resolved"
                or facts.approval_decisions != ("reject",)
            ):
                raise RuntimeError("approval SLA did not expire durably")
            if facts.action_execution_count != 0:
                raise RuntimeError("expired approval attempted the action")
        elif scenario == "negative-duplicate-submission":
            if facts.approval_resolve_statuses != (200, 409):
                raise RuntimeError(
                    "duplicate approval did not produce one success and one conflict"
                )
        elif scenario == "negative-cancellation-after-approval":
            if not facts.cancelled or facts.run_status not in {"failed", "cancelled"}:
                raise RuntimeError("post-approval cancellation was not durable")
            if facts.action_execution_count != 0:
                raise RuntimeError("post-approval cancellation did not fence the action")
        elif scenario == "negative-restart-around-receipt":
            if not facts.restarted or facts.run_status != "succeeded":
                raise RuntimeError("receipt did not survive the controlled backend restart")
        elif scenario == "negative-sink-unavailable":
            if facts.action_execution_count != 1 or facts.operation_state != "failed":
                raise RuntimeError("unavailable sink did not fail exactly one action attempt")
        elif scenario == "negative-timeout-after-commit":
            if facts.outcome_lookup_count != 1:
                raise RuntimeError(
                    "timeout-after-commit did not perform exactly one outcome lookup"
                )
            if facts.run_status != "succeeded":
                raise RuntimeError("authoritative committed outcome did not complete the run")
        elif scenario == "negative-ambiguous-no-reexecution":
            if (
                facts.action_execution_count != 1
                or facts.outcome_lookup_count != 1
                or facts.operation_state != "ambiguous"
            ):
                raise RuntimeError(
                    "unresolved outcome was not retained as ambiguous after one lookup"
                )
