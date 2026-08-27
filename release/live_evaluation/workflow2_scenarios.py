"""Fail-closed Workflow 2 negative acceptance and local control boundaries.

The evaluator validates observations; it never manufactures missing runtime or
browser evidence.  The local controller intentionally supports only the public
operator-cancellation boundary.  Branch-scoped pause/failure injection and UI
refresh restoration remain blocked until a real runtime or browser controller
can produce authoritative evidence.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from math import isfinite
from typing import Any, Protocol

import httpx

from .browser_refresh import BoundedRefreshEvidenceProducer, BrowserRefreshEvidence
from .campaign_execution import WorkflowAction
from .campaign_http import BackendObservation
from .coordinator import ActionRecorder, CriterionResult, StepResult


class UnsupportedWorkflow2ScenarioError(RuntimeError):
    """The campaign has no safe deterministic controller for this scenario."""


@dataclass(frozen=True, slots=True)
class PreparedWorkflow2Scenario:
    checkpoint_id: str
    post_submission_action: str
    evidence: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.checkpoint_id or self.post_submission_action != "cancel" or not self.evidence:
            raise ValueError("workflow2 scenario preparation is incomplete")


class Workflow2ScenarioController(Protocol):
    """External seam for deterministic controls that must occur after submission."""

    def prepare(
        self, action: WorkflowAction, recorder: ActionRecorder
    ) -> PreparedWorkflow2Scenario: ...

    def after_submission(
        self,
        action: WorkflowAction,
        prepared: PreparedWorkflow2Scenario,
        *,
        run_id: str,
        base_url: str,
        request: Callable[..., object],
        recorder: ActionRecorder,
    ) -> tuple[str, ...]: ...


class LocalWorkflow2ScenarioController:
    """Remote campaign controller for exact W2 cancellation binding.

    Refresh remains browser-owned: this class never relabels HTTP reads as UI
    restoration evidence.
    """

    def __init__(
        self,
        *,
        controller_url: str | None = None,
        controller_key: str | None = None,
        workflow_id: str | None = None,
        client: httpx.Client | None = None,
        refresh_producer: BoundedRefreshEvidenceProducer | None = None,
    ) -> None:
        self.controller_url = controller_url.rstrip("/") if controller_url else None
        self.controller_key = controller_key
        self.workflow_id = workflow_id
        self.client = client
        self.refresh_producer = refresh_producer

    def execute_refresh(
        self, action: WorkflowAction, recorder: ActionRecorder
    ) -> BrowserRefreshEvidence:
        if action.scenario != "negative-refresh-restoration":
            raise UnsupportedWorkflow2ScenarioError("incompatible browser refresh scenario")
        if self.refresh_producer is None:
            raise UnsupportedWorkflow2ScenarioError(
                "refresh restoration requires the bounded Playwright producer; fail closed"
            )
        return self.refresh_producer.run("w2_refresh_restoration", recorder=recorder)

    def _controller_request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if (
            self.controller_url is None
            or self.controller_key is None
            or self.workflow_id is None
            or self.client is None
        ):
            raise UnsupportedWorkflow2ScenarioError(
                "workflow2 remote controller is not configured; fail closed"
            )
        response = self.client.request(
            method,
            f"{self.controller_url}{path}",
            headers={"X-Controller-Key": self.controller_key},
            timeout=10.0,
            **kwargs,
        )
        if response.status_code < 200 or response.status_code >= 300:
            raise RuntimeError("workflow2 remote scenario control failed")
        return response

    def prepare(
        self, action: WorkflowAction, recorder: ActionRecorder
    ) -> PreparedWorkflow2Scenario:
        if action.workflow != "workflow2" or action.action_type != "negative":
            raise ValueError("workflow2 controller received an incompatible action")
        if action.scenario != "negative-cancellation":
            raise UnsupportedWorkflow2ScenarioError(
                f"{action.scenario} requires a targeted browser producer; fail closed"
            )
        response = self._controller_request(
            "POST",
            "/v1/scenarios/prepare",
            json={
                "scenario_id": "w2_cancellation",
                "workflow_id": self.workflow_id,
                "expected": {
                    "run_status": "cancelled",
                    "marker_count": 0,
                    "reexecution_count": 0,
                },
            },
        )
        body = response.json()
        fixture_id = body.get("fixture_id") if isinstance(body, dict) else None
        refs = body.get("evidence") if isinstance(body, dict) else None
        if (
            not isinstance(fixture_id, str)
            or not isinstance(refs, list)
            or not all(isinstance(ref, str) and ref for ref in refs)
        ):
            raise RuntimeError("workflow2 controller preparation response is malformed")
        evidence = recorder.record_api_result(
            method="POST",
            path="/v1/scenarios/prepare",
            status_code=response.status_code,
            metadata={"scenario": action.scenario},
        )
        return PreparedWorkflow2Scenario(
            checkpoint_id=fixture_id,
            post_submission_action="cancel",
            evidence=(*refs, evidence),
        )

    def after_submission(
        self,
        action: WorkflowAction,
        prepared: PreparedWorkflow2Scenario,
        *,
        run_id: str,
        base_url: str,
        request: Callable[..., object],
        recorder: ActionRecorder,
    ) -> tuple[str, ...]:
        checkpoint = self._controller_request(
            "POST",
            f"/v1/scenarios/{prepared.checkpoint_id}/checkpoints/run_submitted",
        )
        checkpoint_body = checkpoint.json()
        if not isinstance(checkpoint_body, dict) or checkpoint_body.get("run_id") != run_id:
            raise RuntimeError("workflow2 controller bound a different submitted run")
        cancelled = request(
            "POST",
            f"{base_url}/admin/runs/{run_id}/cancel",
            expected={200},
        )
        body = cancelled.json()
        if (
            not isinstance(body, dict)
            or body.get("run_id") != run_id
            or body.get("status") not in {"failed", "cancelled"}
        ):
            raise RuntimeError("workflow2 operator cancellation was not authoritative")
        return (
            str(checkpoint_body["evidence"]),
            recorder.record_api_result(
                method="POST",
                path=f"/admin/runs/{run_id}/cancel",
                status_code=cancelled.status_code,
                metadata={"controller_fixture_id": prepared.checkpoint_id},
            ),
        )


class Workflow2NegativeEvaluator:
    """Validate all eight Workflow 2 negative cases without optimistic passes."""

    _INPUT_ISSUES = {
        "negative-empty-batch": "too_short",
        "negative-over-24-batch": "too_long",
        "negative-malformed-item": "missing",
    }
    _PARTIAL_SCENARIOS = {
        "negative-retrieval-miss": "retrieval_miss",
        "negative-child-pause-partial-collection": "child_paused",
        "negative-child-failure-partial-collection": "child_failed",
    }

    @staticmethod
    def _cost_delta(observation: BackendObservation) -> float:
        value = (observation.cost or {}).get("run_cost_delta_usd")
        if not isinstance(value, (int, float)) or not isfinite(float(value)) or value < 0:
            raise RuntimeError("workflow2 negative lacks a finite nonnegative cost delta")
        return float(value)

    @staticmethod
    def _records(observation: BackendObservation) -> list[dict[str, object]]:
        records = (observation.audits or {}).get("records")
        if (
            not isinstance(records, list)
            or not records
            or not all(isinstance(record, dict) for record in records)
        ):
            raise RuntimeError("workflow2 negative audit records are missing or malformed")
        return records

    @staticmethod
    def _failure_reason(observation: BackendObservation) -> str | None:
        failure = (observation.run or {}).get("failure_state")
        return failure.get("reason") if isinstance(failure, dict) else None

    @staticmethod
    def _ordered_results(observation: BackendObservation) -> list[dict[str, object]]:
        output = (observation.run or {}).get("terminal_output")
        results = output.get("results") if isinstance(output, dict) else None
        if (
            not isinstance(results, list)
            or len(results) != 8
            or not all(isinstance(result, dict) for result in results)
        ):
            raise RuntimeError("workflow2 partial collection must contain eight explicit slots")
        if [result.get("index") for result in results] != list(range(8)):
            raise RuntimeError("workflow2 partial collection is not index ordered")
        return results

    @staticmethod
    def _paused_child_results(observation: BackendObservation) -> list[dict[str, object]]:
        if len(observation.children) != 7:
            raise RuntimeError("workflow2 pause must preserve seven completed child runs")
        results: list[dict[str, object]] = []
        for child in observation.children:
            if child.get("status") != "succeeded":
                raise RuntimeError("workflow2 paused sibling did not complete successfully")
            output = child.get("terminal_output")
            if not isinstance(output, dict) or not isinstance(output.get("index"), int):
                raise RuntimeError("workflow2 paused sibling lacks structured output identity")
            if output.get("error") is not None:
                raise RuntimeError("workflow2 paused sibling reported an unexpected error")
            results.append(output)
        results.sort(key=lambda item: int(item["index"]))
        if [item["index"] for item in results] != [0, 1, 2, 4, 5, 6, 7]:
            raise RuntimeError("workflow2 paused siblings do not prove exactly branch index 3")
        paused = (observation.run or {}).get("approval_paused_state")
        node_id = paused.get("node_id") if isinstance(paused, dict) else None
        if not isinstance(node_id, str) or "branch:3:" not in node_id:
            raise RuntimeError("workflow2 child pause is not correlated to branch index 3")
        return results

    @staticmethod
    def _assert_child_isolation(observation: BackendObservation) -> None:
        children = observation.children
        if not children:
            raise RuntimeError("workflow2 negative lacks authoritative child run observations")
        run_ids = [child.get("run_id") for child in children]
        thread_ids = [child.get("thread_id") for child in children]
        if (
            any(not isinstance(value, str) or not value for value in (*run_ids, *thread_ids))
            or len(run_ids) != len(set(run_ids))
            or len(thread_ids) != len(set(thread_ids))
        ):
            raise RuntimeError("workflow2 child runs are not isolated")

    @staticmethod
    def _assert_audit_economics(records: list[dict[str, object]], delta: float) -> None:
        provider_records: list[dict[str, object]] = []
        operation_ids: list[str] = []
        provider_ids: list[str] = []
        cost_ids: list[str] = []
        provider_cost = 0.0
        for record in records:
            cost = record.get("cost_usd", 0)
            if not isinstance(cost, (int, float)) or not isfinite(float(cost)) or cost < 0:
                raise RuntimeError("workflow2 audit contains an invalid cost")
            metadata = record.get("execution_metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            provider_id = metadata.get("provider_request_id")
            if provider_id is None:
                continue
            operation_id = metadata.get("operation_id")
            cost_id = record.get("cost_event_id")
            if not all(
                isinstance(value, str) and value for value in (provider_id, operation_id, cost_id)
            ):
                raise RuntimeError("workflow2 provider call lacks operation or cost identity")
            provider_records.append(record)
            provider_cost += float(cost)
            provider_ids.append(provider_id)
            operation_ids.append(operation_id)
            cost_ids.append(cost_id)
        if len(operation_ids) != len(set(operation_ids)):
            raise RuntimeError("workflow2 operation identity proves branch reexecution")
        if len(provider_ids) != len(set(provider_ids)) or len(cost_ids) != len(set(cost_ids)):
            raise RuntimeError("workflow2 provider calls are not one-to-one with cost events")
        if provider_records and abs(delta - provider_cost) > max(0.000001, provider_cost * 0.005):
            raise RuntimeError("workflow2 audit and deployment cost delta do not reconcile")
        if not provider_records and abs(delta) > 1e-9:
            raise RuntimeError("workflow2 spend has no provider audit event")

    def _assert_runtime_integrity(self, observation: BackendObservation) -> list[dict[str, object]]:
        records = self._records(observation)
        self._assert_child_isolation(observation)
        self._assert_audit_economics(records, self._cost_delta(observation))
        return records

    def evaluate(self, action: WorkflowAction, observation: BackendObservation) -> StepResult:
        if action.workflow != "workflow2" or action.action_type != "negative":
            raise RuntimeError("workflow2 negative evaluator received an incompatible action")
        if not observation.evidence:
            raise RuntimeError("workflow2 negative has no durable evidence")
        delta = self._cost_delta(observation)
        if action.scenario in self._INPUT_ISSUES:
            submission = observation.submission or {}
            if submission.get("status_code") != 422:
                raise RuntimeError("invalid workflow2 input was not rejected with HTTP 422")
            issue_types = submission.get("issue_types")
            if (
                not isinstance(issue_types, list)
                or self._INPUT_ISSUES[action.scenario] not in issue_types
            ):
                raise RuntimeError("workflow2 input rejection reason did not match the contract")
            if (
                observation.run is not None
                or observation.audits is not None
                or observation.children
            ):
                raise RuntimeError("locally rejected workflow2 input created runtime effects")
            if abs(delta) > 1e-9:
                raise RuntimeError("locally rejected workflow2 input changed deployment spend")
        elif action.scenario in self._PARTIAL_SCENARIOS:
            expected_status = (
                "paused_for_approval"
                if action.scenario == "negative-child-pause-partial-collection"
                else "succeeded"
            )
            if (observation.run or {}).get("status") != expected_status:
                raise RuntimeError("workflow2 partial scenario has the wrong parent status")
            if action.scenario == "negative-child-pause-partial-collection":
                results = self._paused_child_results(observation)
            else:
                results = self._ordered_results(observation)
            successes = [result for result in results if result.get("error") is None]
            failures = [result for result in results if result.get("error") is not None]
            expected_failures = (
                0 if action.scenario == "negative-child-pause-partial-collection" else 1
            )
            if len(successes) != 7 or len(failures) != expected_failures:
                raise RuntimeError("workflow2 partial collection must have seven successful items")
            if failures and failures[0].get("error") != self._PARTIAL_SCENARIOS[action.scenario]:
                raise RuntimeError("workflow2 partial collection has the wrong explicit error")
            if action.scenario == "negative-child-pause-partial-collection" and not isinstance(
                (observation.run or {}).get("approval_paused_state"), dict
            ):
                raise RuntimeError("workflow2 child pause lacks a durable approval pause")
            self._assert_runtime_integrity(observation)
        elif action.scenario == "negative-cancellation":
            if (observation.run or {}).get("status") != "failed" or self._failure_reason(
                observation
            ) != "operator_cancelled":
                raise RuntimeError("workflow2 cancellation was not durably operator-cancelled")
            if len(observation.children) != 2 or any(
                child.get("status") != "succeeded" for child in observation.children
            ):
                raise RuntimeError(
                    "workflow2 cancellation did not preserve exactly two completed child runs"
                )
            self._assert_runtime_integrity(observation)
        elif action.scenario == "negative-refresh-restoration":
            submission = observation.submission or {}
            run_id = (observation.run or {}).get("run_id")
            if (
                (observation.run or {}).get("status") != "succeeded"
                or submission.get("before_refresh_run_id") != run_id
                or submission.get("restored_run_id") != run_id
                or submission.get("keyboard_restoration_passed") is not True
                or not isinstance(submission.get("ui_evidence"), str)
            ):
                raise RuntimeError(
                    "workflow2 refresh did not restore the same run with UI evidence"
                )
            self._ordered_results(observation)
            self._assert_runtime_integrity(observation)
        else:
            raise RuntimeError(f"unsupported workflow2 negative scenario: {action.scenario}")
        return StepResult(
            tuple(
                CriterionResult(criterion_id, "pass", observation.evidence)
                for criterion_id in action.criterion_ids
            )
        )
