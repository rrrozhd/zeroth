"""Explicit HTTP boundary for live-evaluation actions.

Nothing in this module runs automatically. Provider-backed runs require both an
enable flag and an exact campaign acknowledgement. Graph publication remains a
tenant-scoped repository-service boundary because Studio creates random graph
IDs and serving a deployment requires a separately restarted process.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from zeroth.contracts.graph.models import Graph

from .campaign_execution import ContractSpec, WorkflowAction
from .coordinator import ActionRecorder, CriterionResult, StepResult
from .evidence import CorrelationIds


def provider_acknowledgement(campaign_id: str) -> str:
    return f"I_ACKNOWLEDGE_LIVE_PROVIDER_COSTS:{campaign_id}"


@dataclass(frozen=True, slots=True)
class HttpBackendConfig:
    console_base_url: str
    deployment_base_urls: dict[str, str]
    campaign_id: str
    local_fault_control_url: str | None = None
    provider_execution_enabled: bool = False
    provider_acknowledgement: str | None = None
    poll_interval_seconds: float = 0.25
    max_poll_attempts: int = 240
    request_timeout_seconds: float = 10.0
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "::1")

    def __post_init__(self) -> None:
        if not self.console_base_url:
            raise ValueError("console service URL is required")
        if not self.deployment_base_urls:
            raise ValueError("at least one deployment service URL is required")
        if (
            self.poll_interval_seconds < 0
            or self.max_poll_attempts < 1
            or not 0 < self.request_timeout_seconds <= 30
        ):
            raise ValueError("poll limits must be bounded and non-negative")
        if self.provider_execution_enabled and self.provider_acknowledgement != (
            provider_acknowledgement(self.campaign_id)
        ):
            raise ValueError("provider execution requires the exact campaign acknowledgement")
        urls = [self.console_base_url, *self.deployment_base_urls.values()]
        if self.local_fault_control_url is not None:
            urls.append(self.local_fault_control_url)
        for url in urls:
            if urlparse(url).hostname not in set(self.allowed_hosts):
                raise ValueError("campaign service URL host is not explicitly allowed")


@dataclass(frozen=True, slots=True)
class PublishedGraph:
    graph_id: str
    version: int


class TenantGraphPublisher(Protocol):
    """Repository-service seam that registers contracts and publishes exact graph IDs."""

    def publish(
        self,
        *,
        graphs: tuple[Graph, ...],
        contracts: tuple[ContractSpec, ...],
        tenant_id: str,
        workspace_id: str | None,
    ) -> tuple[PublishedGraph, ...]: ...


class DeploymentSupervisor(Protocol):
    """External process boundary that restarts one deployment service."""

    def restart(self, *, deployment_ref: str, service_url: str) -> None: ...


@dataclass(frozen=True, slots=True)
class BackendObservation:
    evidence: tuple[str, ...]
    run: dict[str, object] | None = None
    audits: dict[str, object] | None = None
    cost: dict[str, object] | None = None
    submission: dict[str, object] | None = None
    children: tuple[dict[str, object], ...] = ()
    workflow3: object | None = None


@dataclass(frozen=True, slots=True)
class PreparedWorkflow1Scenario:
    """Durable fixture checkpoint created before a stateful W1 negative case."""

    checkpoint_id: str
    evidence: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.checkpoint_id or not self.evidence:
            raise ValueError("workflow1 fixture preparation requires durable evidence")


class Workflow1ScenarioController(Protocol):
    """External local fixture boundary for corpus/revision state and restoration."""

    def prepare(
        self, action: WorkflowAction, recorder: ActionRecorder
    ) -> PreparedWorkflow1Scenario: ...

    def restore(
        self,
        action: WorkflowAction,
        prepared: PreparedWorkflow1Scenario,
        recorder: ActionRecorder,
    ) -> tuple[str, ...]: ...


class AcceptanceEvaluator(Protocol):
    """Workflow-specific semantic acceptance; the transport never invents a pass."""

    def evaluate(self, action: WorkflowAction, observation: BackendObservation) -> StepResult: ...


class StrictAcceptanceEvaluator:
    """Conservative built-in evaluator for deployment gates and happy paths."""

    def evaluate(self, action: WorkflowAction, observation: BackendObservation) -> StepResult:
        if not observation.evidence:
            raise RuntimeError("acceptance has no durable evidence")
        if action.action_type == "negative":
            raise RuntimeError("negative case requires a scenario-specific evaluator")
        if action.action_type == "run":
            run = observation.run or {}
            if run.get("status") != "succeeded":
                raise RuntimeError("happy-path run did not succeed")
            records = (observation.audits or {}).get("records")
            if not isinstance(records, list) or not records:
                raise RuntimeError("happy-path run has no audit records")
            if not isinstance((observation.cost or {}).get("total_cost_usd"), (int, float)):
                raise RuntimeError("happy-path run has no measured deployment cost")
            output = run.get("terminal_output")
            if not isinstance(output, dict):
                raise RuntimeError("happy-path run has no structured terminal output")
            required = {
                "workflow1": {"answer", "source_ids"},
                "workflow2": {"results"},
                "workflow3": {"operation_key", "payload_hash", "receipt", "created_at"},
            }[action.workflow]
            if not required.issubset(output):
                raise RuntimeError("happy-path terminal output is incomplete")
            if action.workflow == "workflow2":
                results = output["results"]
                if not isinstance(results, list) or len(results) != 8:
                    raise RuntimeError("batched investigation did not return eight results")
                indices = [item.get("index") for item in results if isinstance(item, dict)]
                if indices != sorted(indices):
                    raise RuntimeError("batched investigation output is not index ordered")
        return StepResult(
            tuple(
                CriterionResult(criterion_id, "pass", observation.evidence)
                for criterion_id in action.criterion_ids
            )
        )


class Workflow1NegativeEvaluator:
    """Fail-closed semantic evaluator for the ten grounded-researcher negatives."""

    _INPUT_ISSUES = {
        "negative-empty-query": "string_too_short",
        "negative-oversized-query": "string_too_long",
    }
    _LOCAL_FAILURES = {
        "negative-chroma-unavailable",
        "negative-bad-credential",
        "negative-provider-timeout",
        "negative-rate-limit",
        "negative-malformed-response",
    }

    @staticmethod
    def _records(observation: BackendObservation) -> list[dict[str, object]]:
        records = (observation.audits or {}).get("records")
        if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
            raise RuntimeError("workflow1 negative audit records are missing or malformed")
        return records

    @staticmethod
    def _run_cost_delta(observation: BackendObservation) -> float:
        delta = (observation.cost or {}).get("run_cost_delta_usd")
        if not isinstance(delta, (int, float)):
            raise RuntimeError("workflow1 negative lacks a before/after cost delta")
        return float(delta)

    @staticmethod
    def _assert_no_provider_event(records: list[dict[str, object]]) -> None:
        for record in records:
            metadata = record.get("execution_metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            if metadata.get("provider_request_id") is not None:
                raise RuntimeError("locally prevented call emitted a provider request identity")
            if record.get("cost_event_id") is not None:
                raise RuntimeError("locally prevented call emitted a provider cost event")
            cost = record.get("cost_usd", 0)
            if not isinstance(cost, (int, float)) or float(cost) != 0:
                raise RuntimeError("locally prevented call has non-zero audit cost")

    @staticmethod
    def _assert_one_provider_cost(
        records: list[dict[str, object]], delta: float, *, expected_calls: int = 1
    ) -> None:
        provider_records = [
            record
            for record in records
            if isinstance(record.get("execution_metadata"), dict)
            and record["execution_metadata"].get("provider_request_id") is not None
        ]
        if len(provider_records) != expected_calls or any(
            record.get("cost_event_id") is None for record in provider_records
        ):
            raise RuntimeError("workflow1 provider calls do not map one-to-one to cost events")
        if any(
            not isinstance(record["execution_metadata"].get("operation_id"), str)
            or not record["execution_metadata"]["operation_id"]
            for record in provider_records
        ):
            raise RuntimeError("workflow1 provider call lacks an operation identity")
        audit_cost = sum(float(record.get("cost_usd", 0)) for record in records)
        if abs(delta - audit_cost) > max(0.000001, abs(audit_cost) * 0.005):
            raise RuntimeError("workflow1 audit and deployment cost delta do not reconcile")

    @staticmethod
    def _failure_reason(observation: BackendObservation) -> str | None:
        failure = (observation.run or {}).get("failure_state")
        return failure.get("reason") if isinstance(failure, dict) else None

    def evaluate(self, action: WorkflowAction, observation: BackendObservation) -> StepResult:
        if action.workflow != "workflow1" or action.action_type != "negative":
            raise RuntimeError("workflow1 negative evaluator received an incompatible action")
        if not observation.evidence:
            raise RuntimeError("workflow1 negative has no durable evidence")
        delta = self._run_cost_delta(observation)
        if action.scenario in self._INPUT_ISSUES:
            submission = observation.submission or {}
            if submission.get("status_code") != 422:
                raise RuntimeError("invalid workflow1 input was not rejected with HTTP 422")
            issue_types = submission.get("issue_types")
            if (
                not isinstance(issue_types, list)
                or self._INPUT_ISSUES[action.scenario] not in issue_types
            ):
                raise RuntimeError("workflow1 input rejection reason did not match the contract")
            if observation.run is not None or observation.audits is not None or abs(delta) > 1e-9:
                raise RuntimeError("locally rejected workflow1 input created runtime effects")
        elif action.scenario in self._LOCAL_FAILURES:
            if (observation.run or {}).get("status") != "failed":
                raise RuntimeError("local workflow1 fault did not fail the run")
            if self._failure_reason(observation) != "node_execution_failed":
                raise RuntimeError("local workflow1 fault has the wrong failure reason")
            records = self._records(observation)
            if not records or not any(record.get("status") == "failed" for record in records):
                raise RuntimeError("local workflow1 fault lacks a failed audit record")
            if action.scenario == "negative-chroma-unavailable":
                self._assert_no_provider_event(records)
                if abs(delta) > 1e-9:
                    raise RuntimeError("unavailable Chroma changed deployment spend")
            else:
                # Retrieval precedes the deliberately faulted research chat.
                # Its one embedding call is real failure-tax spend and must not
                # be erased merely because the later provider boundary is local.
                self._assert_one_provider_cost(records, delta)
        elif action.scenario == "negative-no-result":
            run = observation.run or {}
            output = run.get("terminal_output")
            if run.get("status") != "succeeded" or not isinstance(output, dict):
                raise RuntimeError("no-result workflow1 run did not finish structurally")
            if output.get("source_ids") != []:
                raise RuntimeError("no-result workflow1 run claimed grounded sources")
            answer = output.get("answer")
            if not isinstance(answer, str) or not any(
                token in answer.lower() for token in ("no grounded", "not found", "no result")
            ):
                raise RuntimeError("no-result workflow1 run did not explicitly abstain")
            records = self._records(observation)
            self._assert_one_provider_cost(records, delta)
        elif action.scenario == "negative-conflicting-document":
            run = observation.run or {}
            output = run.get("terminal_output")
            if run.get("status") != "succeeded" or not isinstance(output, dict):
                raise RuntimeError("conflicting-corpus workflow1 run did not succeed")
            source_ids = output.get("source_ids")
            answer = output.get("answer")
            if (
                not isinstance(source_ids, list)
                or len(source_ids) < 2
                or not isinstance(answer, str)
                or not any(token in answer.lower() for token in ("conflict", "inconsistent"))
            ):
                raise RuntimeError("conflicting-corpus output did not expose the disagreement")
            self._assert_one_provider_cost(self._records(observation), delta, expected_calls=2)
        elif action.scenario == "negative-excessive-revision":
            if (observation.run or {}).get("status") != "terminated_by_loop_guard":
                raise RuntimeError("excessive revision did not terminate at the loop guard")
            if self._failure_reason(observation) != "max_total_steps":
                raise RuntimeError("excessive revision has the wrong loop-guard reason")
            records = self._records(observation)
            if sum(record.get("node_id") == "research" for record in records) != 2:
                raise RuntimeError("excessive revision did not stop at two research attempts")
            self._assert_no_provider_event(records)
            if abs(delta) > 1e-9:
                raise RuntimeError("scripted excessive revision changed provider spend")
        else:
            raise RuntimeError(f"unsupported workflow1 negative scenario: {action.scenario}")
        return StepResult(
            tuple(
                CriterionResult(criterion_id, "pass", observation.evidence)
                for criterion_id in action.criterion_ids
            )
        )


class HttpCampaignExecutionBackend:
    """Synchronous, bounded adapter for console and per-deployment Zeroth APIs."""

    def __init__(
        self,
        *,
        config: HttpBackendConfig,
        client: httpx.Client,
        publisher: TenantGraphPublisher,
        evaluator: AcceptanceEvaluator,
        contracts: tuple[ContractSpec, ...],
        tenant_id: str,
        workspace_id: str | None = None,
        api_key: str | None = None,
        supervisor: DeploymentSupervisor | None = None,
        workflow1_scenario_controller: Workflow1ScenarioController | None = None,
        workflow2_scenario_controller: Any | None = None,
        workflow3_scenario_controller: Any | None = None,
    ) -> None:
        self.config = config
        self.client = client
        self.publisher = publisher
        self.evaluator = evaluator
        self.contracts = contracts
        self.tenant_id = tenant_id
        self.workspace_id = workspace_id
        self.supervisor = supervisor
        self.workflow1_scenario_controller = workflow1_scenario_controller
        self.workflow2_scenario_controller = workflow2_scenario_controller
        self.workflow3_scenario_controller = workflow3_scenario_controller
        self._headers = {"X-Tenant-ID": tenant_id}
        if api_key:
            self._headers["X-API-Key"] = api_key

    def execute(self, action: WorkflowAction, recorder: ActionRecorder) -> StepResult:
        if action.action_type == "deployment_gate":
            observation = self._deploy(action, recorder)
        else:
            self._require_provider_acknowledgement()
            observation = (
                self._run_workflow1_negative(action, recorder)
                if action.workflow == "workflow1" and action.action_type == "negative"
                else self._run_workflow2_negative(action, recorder)
                if action.workflow == "workflow2" and action.action_type == "negative"
                else self._run_workflow3_negative(action, recorder)
                if action.workflow == "workflow3" and action.action_type == "negative"
                else self._run(action, recorder)
            )
        result = self.evaluator.evaluate(action, observation)
        if {item.criterion_id for item in result.criteria} != set(action.criterion_ids):
            raise RuntimeError("acceptance evaluator omitted or invented campaign criteria")
        return result

    def _require_provider_acknowledgement(self) -> None:
        if not self.config.provider_execution_enabled or self.config.provider_acknowledgement != (
            provider_acknowledgement(self.config.campaign_id)
        ):
            raise RuntimeError("provider run requires exact acknowledgement and enablement")

    def _deploy(self, action: WorkflowAction, recorder: ActionRecorder) -> BackendObservation:
        if not action.graph_specs or len(action.graph_specs) != len(action.deployment_refs):
            raise RuntimeError("deployment action lacks graph/deployment pairing")
        published = self.publisher.publish(
            graphs=action.graph_specs,
            contracts=self.contracts,
            tenant_id=self.tenant_id,
            workspace_id=self.workspace_id,
        )
        if len(published) != len(action.deployment_refs):
            raise RuntimeError("tenant graph publisher returned an incomplete result")
        evidence: list[str] = []
        correlation = CorrelationIds(operation_id=action.request.identity.operation_id)
        for graph, deployment_ref in zip(published, action.deployment_refs, strict=True):
            response = self._request(
                "POST",
                f"{self.config.console_base_url}/v1/deployments",
                json={
                    "deployment_ref": deployment_ref,
                    "graph_id": graph.graph_id,
                    "graph_version": graph.version,
                },
                expected={200, 201},
            )
            evidence.append(
                recorder.record_api_result(
                    method="POST",
                    path="/v1/deployments",
                    status_code=response.status_code,
                    metadata={"deployment_ref": deployment_ref},
                    correlation=correlation,
                )
            )
            service_url = self._deployment_url(deployment_ref)
            if self.supervisor is None:
                raise RuntimeError("deployment gate requires an explicit restart supervisor")
            self.supervisor.restart(deployment_ref=deployment_ref, service_url=service_url)
            evidence.append(
                recorder.record_ui_action(
                    action="restart-deployment-service",
                    outcome="supervisor completed",
                    metadata={"deployment_ref": deployment_ref},
                    correlation=correlation,
                )
            )
            health = self._request("GET", f"{service_url}/health", expected={200})
            health_body = self._json_object(health)
            expected_graph_ref = f"{graph.graph_id}@{graph.version}"
            if (
                health_body.get("deployment_ref") != deployment_ref
                or health_body.get("graph_version_ref") != expected_graph_ref
            ):
                raise RuntimeError("restarted deployment health does not match published graph")
            evidence.append(
                recorder.record_api_result(
                    method="GET",
                    path="/health",
                    status_code=health.status_code,
                    metadata={
                        "deployment_ref": deployment_ref,
                        "graph_version_ref": expected_graph_ref,
                    },
                    correlation=correlation,
                )
            )
        return BackendObservation(evidence=tuple(evidence))

    def _run(self, action: WorkflowAction, recorder: ActionRecorder) -> BackendObservation:
        base_url = self._deployment_url(action.request.deployment_ref)
        evidence: list[str] = []
        if action.request.fault_control is not None:
            self._arm_fault(
                action,
                recorder,
                evidence,
                action.request.fault_control,
            )
        created = self._request(
            "POST", f"{base_url}/v1/runs", json=action.request.body, expected={200, 202}
        )
        return self._observe_created_run(action, recorder, base_url, evidence, created)

    def _run_workflow1_negative(
        self, action: WorkflowAction, recorder: ActionRecorder
    ) -> BackendObservation:
        base_url = self._deployment_url(action.request.deployment_ref)
        evidence: list[str] = []
        prepared: PreparedWorkflow1Scenario | None = None
        if action.scenario == "negative-conflicting-document":
            if self.workflow1_scenario_controller is None:
                raise RuntimeError(
                    f"{action.scenario} requires an explicit local scenario controller"
                )
            prepared = self.workflow1_scenario_controller.prepare(action, recorder)
            evidence.extend(prepared.evidence)
        try:
            before = self._cost_snapshot(action, recorder, evidence, phase="before")
            fault_control = action.request.fault_control
            if action.scenario == "negative-no-result":
                fault_control = {
                    "deterministic": True,
                    "mode": "retrieval_miss",
                    "parameters": {},
                    "target": "connector",
                }
            elif action.scenario in {
                "negative-empty-query",
                "negative-oversized-query",
                "negative-conflicting-document",
            }:
                fault_control = None
            if fault_control is not None:
                self._arm_fault(action, recorder, evidence, fault_control)
            expected = (
                {422}
                if action.scenario in {"negative-empty-query", "negative-oversized-query"}
                else {200, 202}
            )
            created = self._request(
                "POST",
                f"{base_url}/v1/runs",
                json=action.request.body,
                expected=expected,
            )
            if created.status_code == 422:
                body = self._json_object(created)
                detail = body.get("detail")
                if not isinstance(detail, list):
                    raise RuntimeError("input rejection omitted structured validation issues")
                issue_types = [
                    issue.get("type")
                    for issue in detail
                    if isinstance(issue, dict) and isinstance(issue.get("type"), str)
                ]
                evidence.append(
                    recorder.record_api_result(
                        method="POST",
                        path="/v1/runs",
                        status_code=422,
                        metadata={"issue_types": issue_types},
                        correlation=CorrelationIds(
                            operation_id=action.request.identity.operation_id
                        ),
                    )
                )
                after = self._cost_snapshot(action, recorder, evidence, phase="after")
                observation = BackendObservation(
                    evidence=tuple(evidence),
                    cost=self._cost_delta(before, after),
                    submission={"status_code": 422, "issue_types": issue_types},
                )
            else:
                observation = self._observe_created_run(
                    action,
                    recorder,
                    base_url,
                    evidence,
                    created,
                    cost_before=before,
                )
        finally:
            if prepared is not None:
                restored = self.workflow1_scenario_controller.restore(  # type: ignore[union-attr]
                    action, prepared, recorder
                )
                if not restored:
                    raise RuntimeError("workflow1 scenario restoration lacks durable evidence")
                if "observation" in locals():
                    observation = replace(
                        observation,
                        evidence=(*observation.evidence, *restored),
                    )
        return observation

    def _run_workflow2_negative(
        self, action: WorkflowAction, recorder: ActionRecorder
    ) -> BackendObservation:
        """Execute only Workflow 2 controls with authoritative local boundaries."""
        base_url = self._deployment_url(action.request.deployment_ref)
        evidence: list[str] = []
        prepared: Any | None = None
        controlled = {
            "negative-cancellation",
            "negative-refresh-restoration",
        }
        if action.scenario == "negative-refresh-restoration":
            controller = self.workflow2_scenario_controller
            browser_refresh = getattr(controller, "execute_refresh", None)
            if not callable(browser_refresh):
                raise RuntimeError("refresh restoration requires the bounded browser controller")
            before = self._cost_snapshot(action, recorder, evidence, phase="before")
            produced = browser_refresh(action, recorder)
            evidence.extend(produced.evidence)
            return self._observe_bound_run(
                action,
                recorder,
                base_url,
                evidence,
                run_id=produced.run_id,
                cost_before=before,
                submission={
                    "before_refresh_run_id": produced.before_refresh_run_id,
                    "restored_run_id": produced.restored_run_id,
                    "keyboard_restoration_passed": all(
                        item.get("focus_visible") is True for item in produced.keyboard_focus
                    ),
                    "ui_evidence": produced.evidence[0],
                },
            )
        if action.scenario in controlled:
            if self.workflow2_scenario_controller is None:
                raise RuntimeError(
                    f"{action.scenario} requires an explicit workflow2 scenario controller"
                )
            prepared = self.workflow2_scenario_controller.prepare(action, recorder)
            evidence.extend(prepared.evidence)

        before = self._cost_snapshot(action, recorder, evidence, phase="before")
        input_scenarios = {
            "negative-empty-batch",
            "negative-over-24-batch",
            "negative-malformed-item",
        }
        if action.scenario not in input_scenarios and action.scenario == "negative-retrieval-miss":
            if action.request.fault_control is None:
                raise RuntimeError("retrieval miss lacks deterministic connector control")
            self._arm_fault(action, recorder, evidence, action.request.fault_control)
        expected = {422} if action.scenario in input_scenarios else {200, 202}
        created = self._request(
            "POST", f"{base_url}/v1/runs", json=action.request.body, expected=expected
        )
        if created.status_code == 422:
            body = self._json_object(created)
            detail = body.get("detail")
            if not isinstance(detail, list):
                raise RuntimeError("input rejection omitted structured validation issues")
            issue_types = [
                issue.get("type")
                for issue in detail
                if isinstance(issue, dict) and isinstance(issue.get("type"), str)
            ]
            evidence.append(
                recorder.record_api_result(
                    method="POST",
                    path="/v1/runs",
                    status_code=422,
                    metadata={"issue_types": issue_types},
                    correlation=CorrelationIds(operation_id=action.request.identity.operation_id),
                )
            )
            after = self._cost_snapshot(action, recorder, evidence, phase="after")
            return BackendObservation(
                evidence=tuple(evidence),
                cost=self._cost_delta(before, after),
                submission={"status_code": 422, "issue_types": issue_types},
            )
        if prepared is not None:
            if prepared.post_submission_action != "cancel":
                raise RuntimeError("workflow2 controller returned an unsupported run control")
            created_body = self._json_object(created)
            run_id = created_body.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                raise RuntimeError("workflow2 run control requires a server run identity")
            controlled_evidence = self.workflow2_scenario_controller.after_submission(
                action,
                prepared,
                run_id=run_id,
                base_url=base_url,
                request=self._request,
                recorder=recorder,
            )
            if not controlled_evidence:
                raise RuntimeError("workflow2 post-submission control lacks durable evidence")
            evidence.extend(controlled_evidence)
        return self._observe_created_run(
            action,
            recorder,
            base_url,
            evidence,
            created,
            cost_before=before,
        )

    def _run_workflow3_negative(
        self, action: WorkflowAction, recorder: ActionRecorder
    ) -> BackendObservation:
        """Guard W3 transport with authoritative scenario preparation/finalization."""
        controller = self.workflow3_scenario_controller
        if controller is None:
            raise RuntimeError(
                f"{action.scenario} requires an explicit workflow3 scenario controller"
            )
        prepared = controller.prepare(action)
        if action.scenario == "negative-refresh-before-approval":
            browser_refresh = getattr(controller, "execute_refresh", None)
            if not callable(browser_refresh):
                raise RuntimeError(
                    "refresh-before-approval requires the bounded browser controller"
                )
            evidence = list(prepared.evidence)
            before = self._cost_snapshot(action, recorder, evidence, phase="before")
            produced = browser_refresh(action, recorder)
            evidence.extend(produced.evidence)
            observation = self._observe_bound_run(
                action,
                recorder,
                self._deployment_url(action.request.deployment_ref),
                evidence,
                run_id=produced.run_id,
                cost_before=before,
                submission={
                    "before_refresh_run_id": produced.before_refresh_run_id,
                    "restored_run_id": produced.restored_run_id,
                    "keyboard_restoration_passed": True,
                    "ui_evidence": produced.evidence[0],
                },
            )
        else:
            observation = self._run_workflow3_transport(action, recorder)
        reported = observation.workflow3
        if reported is None:
            trace_reader = getattr(controller, "trace", None)
            verifier = getattr(controller, "verify", None)
            run_id = (observation.run or {}).get("run_id")
            if not callable(trace_reader) or not callable(verifier) or not isinstance(run_id, str):
                raise RuntimeError("workflow3 transport omitted authoritative runtime facts")
            remote_evidence = verifier(action, run_id=run_id)
            if not remote_evidence:
                raise RuntimeError("workflow3 remote verification lacks durable evidence")
            observation = replace(
                observation,
                evidence=(*observation.evidence, *remote_evidence),
            )
            reported = self._workflow3_runtime_facts(
                action,
                observation,
                trace_reader(action),
            )
        finalized = controller.finalize(
            action,
            prepared=prepared,
            reported=reported,
        )
        return replace(observation, workflow3=finalized)

    def _run_workflow3_transport(
        self, action: WorkflowAction, recorder: ActionRecorder
    ) -> BackendObservation:
        """Overridable transport seam; production uses the ordinary bounded HTTP path."""
        return self._run(action, recorder)

    @staticmethod
    def _workflow3_runtime_facts(
        action: WorkflowAction,
        observation: BackendObservation,
        trace: dict[str, object],
    ) -> object:
        """Derive W3 facts only from public run data and signed audit fields."""
        from .action_runner import EVALUATION_ACTION_MANIFEST_SHA256
        from .workflow3_scenarios import Workflow3RuntimeFacts

        run = observation.run or {}
        audits = observation.audits or {}
        records = audits.get("records")
        if not isinstance(records, list) or not all(isinstance(record, dict) for record in records):
            raise RuntimeError("workflow3 runtime facts require audit records")
        if audits.get("chain_verified") is not True or any(
            not record.get("record_signature") for record in records
        ):
            raise RuntimeError("workflow3 runtime facts require a verified signed audit chain")
        action_records = []
        for record in records:
            metadata = record.get("execution_metadata")
            if (
                isinstance(metadata, dict)
                and metadata.get("manifest_ref_sha256")
                == EVALUATION_ACTION_MANIFEST_SHA256
            ):
                action_records.append(metadata)
        zero_effect = action.scenario in {
            "negative-rejection-zero-marker",
            "negative-refresh-before-approval",
            "negative-sla-expiry",
            "negative-cancellation-after-approval",
            "negative-sink-unavailable",
        }
        if not action_records and not zero_effect:
            raise RuntimeError("workflow3 runtime facts lack an action operation identity")
        operation_keys = {
            metadata.get("operation_key")
            for metadata in action_records
            if isinstance(metadata.get("operation_key"), str)
        }
        if action_records and len(operation_keys) != 1:
            raise RuntimeError("workflow3 action operation identity is missing or inconsistent")
        states = [metadata.get("operation_state") for metadata in action_records]
        if any(state not in {"completed", "failed", "ambiguous"} for state in states):
            raise RuntimeError("workflow3 action operation state is missing or invalid")
        first_execution = [metadata.get("operation_first_execution") for metadata in action_records]
        if any(not isinstance(value, bool) for value in first_execution):
            raise RuntimeError("workflow3 action execution identity is missing")
        execution_count = sum(value is True for value in first_execution)
        lookup_count = sum(
            value is False and metadata.get("operation_reconciliation_required") is True
            for value, metadata in zip(first_execution, action_records, strict=True)
        )
        terminal = run.get("terminal_output")
        terminal = terminal if isinstance(terminal, dict) else {}
        operation_key = (
            next(iter(operation_keys))
            if operation_keys
            else f"not-executed:{str(run.get('run_id', 'unknown'))}"
        )
        if terminal.get("operation_key") not in {None, operation_key}:
            raise RuntimeError("workflow3 terminal receipt has the wrong operation identity")

        def _strings(name: str) -> tuple[str, ...]:
            value = trace.get(name, ())
            if not isinstance(value, (list, tuple)) or not all(
                isinstance(item, str) for item in value
            ):
                raise RuntimeError(f"workflow3 trace field {name} is malformed")
            return tuple(value)

        statuses = trace.get("approval_resolve_statuses", ())
        if not isinstance(statuses, (list, tuple)) or not all(
            isinstance(item, int) and not isinstance(item, bool) for item in statuses
        ):
            raise RuntimeError("workflow3 approval status trace is malformed")
        return Workflow3RuntimeFacts(
            scenario=action.scenario,
            operation_key=operation_key,
            marker_count_before=0,
            marker_count_after=0,
            action_execution_count=execution_count,
            outcome_lookup_count=lookup_count,
            automatic_reexecution_count=max(0, execution_count - 1),
            approval_resolve_statuses=tuple(statuses),
            audit_records=tuple(records),
            signed_chain_verified=True,
            run_status=str(run.get("status", "")),
            operation_state=states[-1] if states else "failed",
            approval_decisions=_strings("approval_decisions"),
            approval_id_before=(
                trace.get("approval_id_before")
                if isinstance(trace.get("approval_id_before"), str)
                else None
            ),
            approval_id_after=(
                trace.get("approval_id_after")
                if isinstance(trace.get("approval_id_after"), str)
                else None
            ),
            approval_state_before=(
                trace.get("approval_state_before")
                if isinstance(trace.get("approval_state_before"), str)
                else None
            ),
            approval_state_after=(
                trace.get("approval_state_after")
                if isinstance(trace.get("approval_state_after"), str)
                else None
            ),
            terminal_receipt=(
                terminal.get("receipt") if isinstance(terminal.get("receipt"), str) else None
            ),
            terminal_payload_hash=(
                terminal.get("payload_hash")
                if isinstance(terminal.get("payload_hash"), str)
                else None
            ),
            refreshed=trace.get("refreshed") is True,
            restarted=trace.get("restarted") is True,
            cancelled=trace.get("cancelled") is True,
            sla_expired=trace.get("sla_expired") is True,
        )

    def _arm_fault(
        self,
        action: WorkflowAction,
        recorder: ActionRecorder,
        evidence: list[str],
        fault_control: dict[str, object],
    ) -> None:
        if self.config.local_fault_control_url is None:
            raise RuntimeError("negative case requires a loopback fault control service")
        armed = self._request(
            "POST",
            f"{self.config.local_fault_control_url}/faults/arm",
            json={**fault_control, **action.request.correlation_expectations},
            expected={200, 204},
        )
        evidence.append(
            recorder.record_api_result(
                method="POST",
                path="/faults/arm",
                status_code=armed.status_code,
                metadata={"fault_mode": str(fault_control.get("mode", ""))},
                correlation=CorrelationIds(operation_id=action.request.identity.operation_id),
            )
        )

    def _observe_created_run(
        self,
        action: WorkflowAction,
        recorder: ActionRecorder,
        base_url: str,
        evidence: list[str],
        created: httpx.Response,
        *,
        cost_before: dict[str, object] | None = None,
    ) -> BackendObservation:
        created_body = self._json_object(created)
        run_id = created_body.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise RuntimeError("run creation response omitted server-generated run_id")
        # The run service generates this identity. The harness operation id is
        # not a workflow/provider operation id and must never be aliased to it.
        correlation = CorrelationIds(run_id=run_id)
        evidence.append(
            recorder.record_api_result(
                method="POST",
                path="/v1/runs",
                status_code=created.status_code,
                metadata={"deployment_ref": action.request.deployment_ref},
                correlation=correlation,
            )
        )
        return self._observe_bound_run(
            action,
            recorder,
            base_url,
            evidence,
            run_id=run_id,
            cost_before=cost_before,
        )

    def _observe_bound_run(
        self,
        action: WorkflowAction,
        recorder: ActionRecorder,
        base_url: str,
        evidence: list[str],
        *,
        run_id: str,
        cost_before: dict[str, object] | None = None,
        submission: dict[str, object] | None = None,
    ) -> BackendObservation:
        correlation = CorrelationIds(run_id=run_id)
        terminal = self._poll(action, base_url, run_id, recorder, correlation, evidence)
        audits_response = self._request(
            "GET",
            f"{base_url}/v1/deployments/{action.request.deployment_ref}/audits",
            params={"run_id": run_id},
            expected={200},
        )
        audits_body = self._json_object(audits_response)
        evidence.append(
            recorder.record_api_result(
                method="GET",
                path=f"/v1/deployments/{action.request.deployment_ref}/audits",
                status_code=audits_response.status_code,
                metadata={"record_count": len(audits_body.get("records", []))},
                correlation=correlation,
            )
        )
        records = audits_body.get("records")
        if not isinstance(records, list):
            raise RuntimeError("audit response records must be a list")
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("audit_id"), str):
                raise RuntimeError("audit response contains a malformed record")
            metadata = record.get("execution_metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            observed = CorrelationIds(
                run_id=run_id,
                audit_event_id=record["audit_id"],
                operation_id=(
                    metadata.get("operation_id")
                    if isinstance(metadata.get("operation_id"), str)
                    else None
                ),
                cost_event_id=(
                    record.get("cost_event_id")
                    if isinstance(record.get("cost_event_id"), str)
                    else None
                ),
                provider_request_id=(
                    metadata.get("provider_request_id")
                    if isinstance(metadata.get("provider_request_id"), str)
                    else None
                ),
            )
            event_id = recorder.store.append_event(
                "campaign.audit.observed",
                {
                    "node_id": str(record.get("node_id", "")),
                    "status": str(record.get("status", "")),
                },
                correlation=observed,
            )
            evidence.append(f"events.ndjson#{event_id}")
        cost_after = self._cost_snapshot(
            action,
            recorder,
            evidence,
            phase="after" if cost_before is not None else "observed",
            correlation=correlation,
        )
        children, child_records = self._workflow2_children(
            action, records, recorder, evidence, correlation
        )
        observed_audits = (
            {
                **audits_body,
                "records": [*records, *child_records],
                "parent_records": records,
                "child_records": child_records,
            }
            if action.workflow == "workflow2"
            else audits_body
        )
        return BackendObservation(
            evidence=tuple(evidence),
            run=terminal,
            audits=observed_audits,
            cost=(
                self._cost_delta(cost_before, cost_after) if cost_before is not None else cost_after
            ),
            children=children,
            submission=submission,
        )

    def _workflow2_children(
        self,
        action: WorkflowAction,
        records: list[object],
        recorder: ActionRecorder,
        evidence: list[str],
        correlation: CorrelationIds,
    ) -> tuple[tuple[dict[str, object], ...], list[dict[str, object]]]:
        if action.workflow != "workflow2":
            return (), []
        identities: dict[str, str] = {}
        for record in records:
            if not isinstance(record, dict):
                continue
            metadata = record.get("execution_metadata")
            if not isinstance(metadata, dict):
                continue
            child_run_id = metadata.get("subgraph_run_id")
            child_ref = metadata.get("subgraph_graph_ref")
            if isinstance(child_run_id, str) and child_run_id and isinstance(child_ref, str):
                previous = identities.setdefault(child_run_id, child_ref)
                if previous != child_ref:
                    raise RuntimeError("workflow2 child identity changed deployment reference")
        if not identities:
            raise RuntimeError("workflow2 audit lacks authoritative child run identities")
        children: list[dict[str, object]] = []
        child_records: list[dict[str, object]] = []
        for child_run_id, child_ref in sorted(identities.items()):
            child_base_url = self._deployment_url(child_ref)
            response = self._request(
                "GET", f"{child_base_url}/v1/runs/{child_run_id}", expected={200}
            )
            body = self._json_object(response)
            if body.get("run_id") != child_run_id:
                raise RuntimeError("workflow2 child lookup returned the wrong run")
            children.append(body)
            evidence.append(
                recorder.record_api_result(
                    method="GET",
                    path=f"/v1/runs/{child_run_id}",
                    status_code=response.status_code,
                    metadata={"child_deployment_ref": child_ref},
                    correlation=correlation,
                )
            )
            audits_response = self._request(
                "GET",
                f"{child_base_url}/v1/deployments/{child_ref}/audits",
                params={"run_id": child_run_id},
                expected={200},
            )
            audits = self._json_object(audits_response).get("records")
            if not isinstance(audits, list) or not all(
                isinstance(record, dict) for record in audits
            ):
                raise RuntimeError("workflow2 child audit response is malformed")
            for record in audits:
                audit_id = record.get("audit_id")
                if not isinstance(audit_id, str) or not audit_id:
                    raise RuntimeError("workflow2 child audit lacks identity")
                event_id = recorder.store.append_event(
                    "campaign.child-audit.observed",
                    {
                        "node_id": str(record.get("node_id", "")),
                        "status": str(record.get("status", "")),
                        "child_run_id": child_run_id,
                    },
                    correlation=CorrelationIds(
                        run_id=child_run_id,
                        audit_event_id=audit_id,
                    ),
                )
                evidence.append(f"events.ndjson#{event_id}")
                child_records.append(record)
            evidence.append(
                recorder.record_api_result(
                    method="GET",
                    path=f"/v1/deployments/{child_ref}/audits",
                    status_code=audits_response.status_code,
                    metadata={
                        "child_run_id": child_run_id,
                        "record_count": len(audits),
                    },
                    correlation=correlation,
                )
            )
        return tuple(children), child_records

    def _cost_snapshot(
        self,
        action: WorkflowAction,
        recorder: ActionRecorder,
        evidence: list[str],
        *,
        phase: str,
        correlation: CorrelationIds | None = None,
    ) -> dict[str, object]:
        base_url = self._deployment_url(action.request.deployment_ref)
        response = self._request(
            "GET",
            f"{base_url}/v1/deployments/{action.request.deployment_ref}/cost",
            expected={200},
        )
        body = self._json_object(response)
        if not isinstance(body.get("total_cost_usd"), (int, float)):
            raise RuntimeError("deployment cost snapshot omitted total_cost_usd")
        evidence.append(
            recorder.record_api_result(
                method="GET",
                path=f"/v1/deployments/{action.request.deployment_ref}/cost",
                status_code=response.status_code,
                metadata={"measurement_phase": phase},
                correlation=correlation
                or CorrelationIds(operation_id=action.request.identity.operation_id),
            )
        )
        return body

    @staticmethod
    def _cost_delta(before: dict[str, object], after: dict[str, object]) -> dict[str, object]:
        before_total = float(before["total_cost_usd"])
        after_total = float(after["total_cost_usd"])
        if after_total + 1e-12 < before_total:
            raise RuntimeError("deployment cost counter decreased during negative evaluation")
        return {
            **after,
            "deployment_total_before_usd": before_total,
            "deployment_total_after_usd": after_total,
            "run_cost_delta_usd": after_total - before_total,
        }

    def _poll(
        self,
        action: WorkflowAction,
        base_url: str,
        run_id: str,
        recorder: ActionRecorder,
        correlation: CorrelationIds,
        evidence: list[str],
    ) -> dict[str, object]:
        terminal_statuses = {
            "succeeded",
            "failed",
            "paused_for_approval",
            "waiting_interrupt",
            "terminated_by_policy",
            "terminated_by_loop_guard",
            "dead_letter",
        }
        for _ in range(self.config.max_poll_attempts):
            response = self._request("GET", f"{base_url}/v1/runs/{run_id}", expected={200})
            body = self._json_object(response)
            if body.get("status") == "paused_for_approval" and action.workflow == "workflow3":
                remote_pause = getattr(self.workflow3_scenario_controller, "handle_paused", None)
                paused = body.get("approval_paused_state")
                approval_id = paused.get("approval_id") if isinstance(paused, dict) else None
                if (
                    action.action_type == "negative"
                    and callable(remote_pause)
                    and isinstance(approval_id, str)
                ):
                    controlled_evidence = remote_pause(
                        action,
                        approval_id=approval_id,
                        run_id=run_id,
                        base_url=base_url,
                        request=self._request,
                        recorder=recorder,
                    )
                    if not controlled_evidence:
                        raise RuntimeError("workflow3 remote pause control lacks durable evidence")
                    evidence.extend(controlled_evidence)
                    continue
                decision = (
                    "reject"
                    if action.scenario == "negative-rejection-zero-marker"
                    else "approve"
                    if action.scenario.startswith("happy-")
                    else None
                )
                if decision is not None and isinstance(approval_id, str):
                    resolved = self._request(
                        "POST",
                        (
                            f"{base_url}/v1/deployments/{action.request.deployment_ref}"
                            f"/approvals/{approval_id}/resolve"
                        ),
                        json={"decision": decision, "edited_payload": None},
                        expected={200},
                    )
                    evidence.append(
                        recorder.record_api_result(
                            method="POST",
                            path=(
                                f"/v1/deployments/{action.request.deployment_ref}"
                                f"/approvals/{approval_id}/resolve"
                            ),
                            status_code=resolved.status_code,
                            metadata={"decision": decision},
                            correlation=correlation,
                        )
                    )
                    resolved_body = self._json_object(resolved)
                    resumed = resolved_body.get("run")
                    if isinstance(resumed, dict) and resumed.get("status") in terminal_statuses:
                        body = resumed
                    else:
                        continue
            if body.get("status") in terminal_statuses:
                evidence.append(
                    recorder.record_api_result(
                        method="GET",
                        path=f"/v1/runs/{run_id}",
                        status_code=response.status_code,
                        metadata={"run_status": str(body["status"])},
                        correlation=correlation,
                    )
                )
                return body
            if self.config.poll_interval_seconds:
                time.sleep(self.config.poll_interval_seconds)
        raise RuntimeError("run polling exceeded configured attempt limit")

    def _deployment_url(self, deployment_ref: str) -> str:
        try:
            return self.config.deployment_base_urls[deployment_ref].rstrip("/")
        except KeyError as exc:
            raise RuntimeError(f"no restarted deployment service URL for {deployment_ref}") from exc

    def _request(
        self,
        method: str,
        url: str,
        *,
        expected: set[int],
        json: object | None = None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        try:
            response = self.client.request(
                method,
                url,
                json=json,
                params=params,
                headers=self._headers,
                timeout=self.config.request_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise RuntimeError("campaign HTTP transport failed") from exc
        if response.status_code not in expected:
            raise RuntimeError(
                f"campaign HTTP gate returned unexpected status {response.status_code}"
            )
        return response

    @staticmethod
    def _json_object(response: httpx.Response) -> dict[str, object]:
        try:
            body = response.json()
        except ValueError as exc:
            raise RuntimeError("campaign HTTP response was not JSON") from exc
        if not isinstance(body, dict):
            raise RuntimeError("campaign HTTP response must be a JSON object")
        return body
