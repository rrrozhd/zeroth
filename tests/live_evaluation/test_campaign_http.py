from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from release.live_evaluation.campaign_execution import (
    CampaignExecutionSettings,
    build_campaign_execution,
)
from release.live_evaluation.campaign_http import (
    BackendObservation,
    HttpBackendConfig,
    HttpCampaignExecutionBackend,
    PreparedWorkflow1Scenario,
    PublishedGraph,
    Workflow1NegativeEvaluator,
    provider_acknowledgement,
)
from release.live_evaluation.coordinator import ActionRecorder, CriterionResult, StepResult
from release.live_evaluation.evidence import EvidenceStore


def _execution():
    return build_campaign_execution(
        CampaignExecutionSettings(
            campaign_id="evaluation-studio-v1",
            tenant_id="evaluation-studio-v1",
            model="openai/gpt-4o-mini",
            embedding_model="openai/text-embedding-3-small",
            chroma_connector_ref="eval_chroma_v1",
        )
    )


class _Publisher:
    def __init__(self) -> None:
        self.calls = []

    def publish(self, *, graphs, contracts, tenant_id, workspace_id):
        self.calls.append((graphs, contracts, tenant_id, workspace_id))
        return tuple(PublishedGraph(graph.graph_id, 1) for graph in graphs)


class _Evaluator:
    def evaluate(self, action, observation: BackendObservation) -> StepResult:
        assert observation.evidence
        return StepResult(
            tuple(
                CriterionResult(criterion_id, "pass", observation.evidence)
                for criterion_id in action.criterion_ids
            )
        )


class _Supervisor:
    def __init__(self) -> None:
        self.refs = []

    def restart(self, *, deployment_ref: str, service_url: str) -> None:
        self.refs.append((deployment_ref, service_url))


def test_config_requires_explicit_provider_enablement_ack_and_separate_service_urls() -> None:
    with pytest.raises(ValueError, match="deployment service URL"):
        HttpBackendConfig(
            console_base_url="http://localhost:8000",
            deployment_base_urls={},
            provider_execution_enabled=True,
            provider_acknowledgement="wrong",
            campaign_id="evaluation-studio-v1",
        )

    config = HttpBackendConfig(
        console_base_url="http://localhost:8000",
        deployment_base_urls={"deploy": "http://localhost:8001"},
        provider_execution_enabled=True,
        provider_acknowledgement=provider_acknowledgement("evaluation-studio-v1"),
        campaign_id="evaluation-studio-v1",
    )
    assert config.console_base_url != config.deployment_base_urls["deploy"]


def test_provider_run_refuses_before_http_without_exact_acknowledgement(tmp_path: Path) -> None:
    execution = _execution()
    action = next(item for item in execution.actions if item.scenario == "happy-1")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    backend = HttpCampaignExecutionBackend(
        config=HttpBackendConfig(
            console_base_url="http://localhost:8000",
            deployment_base_urls={action.request.deployment_ref: "http://localhost:8001"},
            campaign_id=execution.settings.campaign_id,
        ),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        publisher=_Publisher(),
        evaluator=_Evaluator(),
        contracts=execution.contracts,
        tenant_id=execution.settings.tenant_id,
    )

    with pytest.raises(RuntimeError, match="acknowledgement"):
        backend.execute(
            action, ActionRecorder(EvidenceStore(tmp_path), step_id="run", command_sequence=1)
        )
    assert calls == 0


def test_deployment_gate_publishes_contracts_deploys_restarts_and_checks_health(
    tmp_path: Path,
) -> None:
    execution = _execution()
    action = next(
        item
        for item in execution.actions
        if item.workflow == "workflow1" and item.action_type == "deployment_gate"
    )
    publisher = _Publisher()
    supervisor = _Supervisor()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.port == 8000:
            return httpx.Response(201, json={"deployment_ref": action.deployment_refs[0]})
        return httpx.Response(
            200,
            json={
                "deployment_ref": action.deployment_refs[0],
                "graph_version_ref": f"{action.graph_specs[0].graph_id}@1",
            },
        )

    backend = HttpCampaignExecutionBackend(
        config=HttpBackendConfig(
            console_base_url="http://localhost:8000",
            deployment_base_urls={action.deployment_refs[0]: "http://localhost:8001"},
            campaign_id=execution.settings.campaign_id,
        ),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        publisher=publisher,
        evaluator=_Evaluator(),
        contracts=execution.contracts,
        tenant_id=execution.settings.tenant_id,
        supervisor=supervisor,
    )

    result = backend.execute(
        action,
        ActionRecorder(EvidenceStore(tmp_path), step_id="deploy", command_sequence=1),
    )

    assert all(item.status == "pass" for item in result.criteria)
    assert publisher.calls[0][1] == execution.contracts
    assert supervisor.refs == [(action.deployment_refs[0], "http://localhost:8001")]


def test_run_uses_exact_schema_polls_and_collects_audit_and_cost(tmp_path: Path) -> None:
    execution = _execution()
    action = next(
        item
        for item in execution.actions
        if item.workflow == "workflow1" and item.scenario == "happy-1"
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/runs" and request.method == "POST":
            body = json.loads(request.content)
            assert set(body) == {"campaign_id", "campaign_strict", "input_payload"}
            return httpx.Response(202, json={"run_id": "run-server-1", "status": "pending"})
        if request.url.path == "/v1/runs/run-server-1":
            return httpx.Response(
                200,
                json={
                    "run_id": "run-server-1",
                    "status": "succeeded",
                    "audit_refs": ["audit-1"],
                    "terminal_output": {"answer": "grounded"},
                },
            )
        if request.url.path.endswith("/audits"):
            return httpx.Response(
                200,
                json={
                    "records": [
                        {
                            "audit_id": "audit-1",
                            "node_id": "research",
                            "status": "completed",
                            "cost_event_id": "cost-1",
                            "execution_metadata": {
                                "operation_id": "provider-op-1",
                                "provider_request_id": "provider-request-1",
                            },
                        }
                    ]
                },
            )
        if request.url.path.endswith("/cost"):
            return httpx.Response(200, json={"total_cost_usd": 0.001})
        raise AssertionError(request.url)

    backend = HttpCampaignExecutionBackend(
        config=HttpBackendConfig(
            console_base_url="http://localhost:8000",
            deployment_base_urls={action.request.deployment_ref: "http://localhost:8001"},
            campaign_id=execution.settings.campaign_id,
            provider_execution_enabled=True,
            provider_acknowledgement=provider_acknowledgement(execution.settings.campaign_id),
            poll_interval_seconds=0,
        ),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        publisher=_Publisher(),
        evaluator=_Evaluator(),
        contracts=execution.contracts,
        tenant_id=execution.settings.tenant_id,
    )

    result = backend.execute(
        action, ActionRecorder(EvidenceStore(tmp_path), step_id="run", command_sequence=1)
    )

    assert all(item.status == "pass" for item in result.criteria)
    assert [request.url.port for request in requests] == [8001] * 4
    assert all(
        request.headers["X-Tenant-ID"] == execution.settings.tenant_id for request in requests
    )
    assert "run-server-1" in (tmp_path / "events.ndjson").read_text()


def test_negative_fault_requires_local_control_endpoint_and_is_armed_before_run(
    tmp_path: Path,
) -> None:
    execution = _execution()
    action = next(item for item in execution.actions if item.scenario == "negative-rate-limit")
    order: list[tuple[str, str, int | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        order.append((request.method, request.url.path, request.url.port))
        if request.url.port is None:
            return httpx.Response(204)
        if request.method == "POST":
            return httpx.Response(202, json={"run_id": "run-fault", "status": "pending"})
        if request.url.path.endswith("/runs/run-fault"):
            return httpx.Response(200, json={"run_id": "run-fault", "status": "failed"})
        if request.url.path.endswith("/audits"):
            return httpx.Response(200, json={"records": []})
        return httpx.Response(200, json={"total_cost_usd": 0})

    backend = HttpCampaignExecutionBackend(
        config=HttpBackendConfig(
            console_base_url="http://localhost:8000",
            deployment_base_urls={action.request.deployment_ref: "http://localhost:8001"},
            local_fault_control_url="http://localhost",
            campaign_id=execution.settings.campaign_id,
            provider_execution_enabled=True,
            provider_acknowledgement=provider_acknowledgement(execution.settings.campaign_id),
            poll_interval_seconds=0,
        ),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        publisher=_Publisher(),
        evaluator=_Evaluator(),
        contracts=execution.contracts,
        tenant_id=execution.settings.tenant_id,
    )

    backend.execute(
        action, ActionRecorder(EvidenceStore(tmp_path), step_id="fault", command_sequence=1)
    )

    fault_index = next(
        index for index, item in enumerate(order) if item[:2] == ("POST", "/faults/arm")
    )
    run_index = next(index for index, item in enumerate(order) if item[:2] == ("POST", "/v1/runs"))
    assert fault_index < run_index


def _w1_negative_backend(execution, action, tmp_path, handler, *, controller=None):
    return HttpCampaignExecutionBackend(
        config=HttpBackendConfig(
            console_base_url="http://localhost:8000",
            deployment_base_urls={action.request.deployment_ref: "http://localhost:8001"},
            local_fault_control_url="http://localhost:8002",
            campaign_id=execution.settings.campaign_id,
            provider_execution_enabled=True,
            provider_acknowledgement=provider_acknowledgement(execution.settings.campaign_id),
            poll_interval_seconds=0,
        ),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        publisher=_Publisher(),
        evaluator=Workflow1NegativeEvaluator(),
        contracts=execution.contracts,
        tenant_id=execution.settings.tenant_id,
        workflow1_scenario_controller=controller,
    )


@pytest.mark.parametrize(
    ("scenario", "issue_type"),
    [
        ("negative-empty-query", "string_too_short"),
        ("negative-oversized-query", "string_too_long"),
    ],
)
def test_workflow1_input_rejections_require_exact_422_and_no_run_side_effects(
    tmp_path: Path, scenario: str, issue_type: str
) -> None:
    execution = _execution()
    action = next(item for item in execution.actions if item.scenario == scenario)
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/cost"):
            return httpx.Response(200, json={"total_cost_usd": 0})
        if request.url.path == "/v1/runs":
            return httpx.Response(
                422,
                json={"detail": [{"type": issue_type, "loc": ["query"]}]},
            )
        raise AssertionError(request.url)

    result = _w1_negative_backend(execution, action, tmp_path, handler).execute(
        action,
        ActionRecorder(EvidenceStore(tmp_path), step_id=scenario, command_sequence=1),
    )

    assert all(item.status == "pass" for item in result.criteria)
    assert "/faults/arm" not in paths
    assert not any(path.endswith("/audits") for path in paths)


@pytest.mark.parametrize(
    "scenario",
    [
        "negative-chroma-unavailable",
        "negative-bad-credential",
        "negative-provider-timeout",
        "negative-rate-limit",
        "negative-malformed-response",
    ],
)
def test_workflow1_local_fault_failures_account_for_prior_retrieval_embedding(
    tmp_path: Path, scenario: str
) -> None:
    execution = _execution()
    action = next(item for item in execution.actions if item.scenario == scenario)
    armed: list[dict[str, object]] = []
    cost_reads = 0
    is_connector_failure = scenario == "negative-chroma-unavailable"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal cost_reads
        if request.url.path == "/faults/arm":
            armed.append(json.loads(request.content))
            return httpx.Response(204)
        if request.url.path.endswith("/cost"):
            cost_reads += 1
            total = 0.04 if is_connector_failure or cost_reads == 1 else 0.041
            return httpx.Response(200, json={"total_cost_usd": total})
        if request.url.path == "/v1/runs" and request.method == "POST":
            return httpx.Response(202, json={"run_id": "run-negative"})
        if request.url.path == "/v1/runs/run-negative":
            return httpx.Response(
                200,
                json={
                    "run_id": "run-negative",
                    "status": "failed",
                    "failure_state": {"reason": "node_execution_failed"},
                },
            )
        if request.url.path.endswith("/audits"):
            records = [
                {
                    "audit_id": "audit-negative",
                    "node_id": "retrieve" if is_connector_failure else "research",
                    "status": "failed",
                    "cost_usd": 0,
                    "execution_metadata": {"evaluation_fault": scenario},
                }
            ]
            if not is_connector_failure:
                records.insert(
                    0,
                    {
                        "audit_id": "audit-retrieval-embedding",
                        "node_id": "retrieve",
                        "status": "completed",
                        "cost_usd": 0.001,
                        "cost_event_id": "cost-retrieval-embedding",
                        "execution_metadata": {
                            "operation_id": "operation-retrieval-embedding",
                            "provider_request_id": "provider-retrieval-embedding",
                        },
                    },
                )
            return httpx.Response(200, json={"records": records})
        raise AssertionError(request.url)

    result = _w1_negative_backend(execution, action, tmp_path, handler).execute(
        action,
        ActionRecorder(EvidenceStore(tmp_path), step_id=scenario, command_sequence=1),
    )

    assert all(item.status == "pass" for item in result.criteria)
    assert len(armed) == 1
    assert cost_reads == 2


def test_workflow1_no_result_uses_connector_miss_and_requires_grounded_abstention(
    tmp_path: Path,
) -> None:
    execution = _execution()
    action = next(item for item in execution.actions if item.scenario == "negative-no-result")
    armed: list[dict[str, object]] = []
    costs = iter((0.04, 0.041))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/faults/arm":
            armed.append(json.loads(request.content))
            return httpx.Response(204)
        if request.url.path.endswith("/cost"):
            return httpx.Response(200, json={"total_cost_usd": next(costs)})
        if request.url.path == "/v1/runs" and request.method == "POST":
            return httpx.Response(202, json={"run_id": "run-no-result"})
        if request.url.path == "/v1/runs/run-no-result":
            return httpx.Response(
                200,
                json={
                    "run_id": "run-no-result",
                    "status": "succeeded",
                    "terminal_output": {
                        "query": "synthetic-no-result",
                        "answer": "No grounded result was found.",
                        "source_ids": [],
                    },
                },
            )
        if request.url.path.endswith("/audits"):
            return httpx.Response(
                200,
                json={
                    "records": [
                        {
                            "audit_id": "audit-retrieve",
                            "node_id": "retrieve",
                            "status": "completed",
                            "cost_usd": 0,
                            "execution_metadata": {},
                        },
                        {
                            "audit_id": "audit-research",
                            "node_id": "research",
                            "status": "completed",
                            "cost_usd": 0.001,
                            "cost_event_id": "cost-no-result",
                            "execution_metadata": {
                                "provider_request_id": "provider-no-result",
                                "operation_id": "provider-op-no-result",
                            },
                        },
                    ]
                },
            )
        raise AssertionError(request.url)

    result = _w1_negative_backend(execution, action, tmp_path, handler).execute(
        action,
        ActionRecorder(EvidenceStore(tmp_path), step_id="no-result", command_sequence=1),
    )

    assert all(item.status == "pass" for item in result.criteria)
    assert armed[0]["target"] == "connector"
    assert armed[0]["mode"] == "retrieval_miss"


class _Workflow1Controller:
    def __init__(self) -> None:
        self.restored: list[str] = []

    def prepare(self, action, recorder) -> PreparedWorkflow1Scenario:
        event_id = recorder.store.append_event(
            "campaign.fixture.prepared",
            {"checkpoint": f"checkpoint:{action.scenario}", "scenario": action.scenario},
        )
        return PreparedWorkflow1Scenario(
            checkpoint_id=f"checkpoint:{action.scenario}",
            evidence=(f"events.ndjson#{event_id}",),
        )

    def restore(self, action, prepared, recorder):
        self.restored.append(prepared.checkpoint_id)
        event_id = recorder.store.append_event(
            "campaign.fixture.restored",
            {"checkpoint": prepared.checkpoint_id, "scenario": action.scenario},
        )
        return (f"events.ndjson#{event_id}",)


@pytest.mark.parametrize(
    ("scenario", "status", "failure_state", "terminal_output"),
    [
        (
            "negative-conflicting-document",
            "succeeded",
            None,
            {
                "answer": "The sources conflict on the synthetic fact.",
                "source_ids": ["conflict-a", "conflict-b"],
            },
        ),
        (
            "negative-excessive-revision",
            "terminated_by_loop_guard",
            {"reason": "max_total_steps"},
            None,
        ),
    ],
)
def test_workflow1_fixture_scenarios_require_checkpoint_and_restore(
    tmp_path: Path,
    scenario: str,
    status: str,
    failure_state: dict[str, object] | None,
    terminal_output: dict[str, object] | None,
) -> None:
    execution = _execution()
    action = next(item for item in execution.actions if item.scenario == scenario)
    controller = _Workflow1Controller()
    cost_values = iter((0.04, 0.0415 if "conflicting" in scenario else 0.04))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/faults/arm":
            payload = json.loads(request.content)
            assert payload["mode"] == "revision_required"
            assert payload["target"] == "provider"
            assert payload["parameters"] == {"uses": 2}
            return httpx.Response(204)
        if request.url.path.endswith("/cost"):
            return httpx.Response(200, json={"total_cost_usd": next(cost_values)})
        if request.url.path == "/v1/runs" and request.method == "POST":
            return httpx.Response(202, json={"run_id": "run-fixture"})
        if request.url.path == "/v1/runs/run-fixture":
            return httpx.Response(
                200,
                json={
                    "run_id": "run-fixture",
                    "status": status,
                    "failure_state": failure_state,
                    "terminal_output": terminal_output,
                },
            )
        if request.url.path.endswith("/audits"):
            records = (
                [
                    {
                        "audit_id": "audit-conflict-embedding",
                        "node_id": "retrieve",
                        "status": "completed",
                        "cost_usd": 0.0005,
                        "cost_event_id": "cost-conflict-embedding",
                        "execution_metadata": {
                            "evaluation_fixture": scenario,
                            "operation_id": "provider-op-conflict-embedding",
                            "provider_request_id": "provider-conflict-embedding",
                        },
                    },
                    {
                        "audit_id": "audit-conflict-research",
                        "node_id": "research",
                        "status": "completed",
                        "cost_usd": 0.001,
                        "cost_event_id": "cost-conflict-research",
                        "execution_metadata": {
                            "evaluation_fixture": scenario,
                            "operation_id": "provider-op-conflict-research",
                            "provider_request_id": "provider-conflict-research",
                        },
                    },
                ]
                if "conflicting" in scenario
                else [
                    {
                        "audit_id": f"audit-revision-{attempt}",
                        "node_id": "research",
                        "status": "completed",
                        "cost_usd": 0,
                        "execution_metadata": {"evaluation_fixture": scenario},
                    }
                    for attempt in (1, 2)
                ]
            )
            return httpx.Response(200, json={"records": records})
        raise AssertionError(request.url)

    result = _w1_negative_backend(
        execution, action, tmp_path, handler, controller=controller
    ).execute(
        action,
        ActionRecorder(EvidenceStore(tmp_path), step_id="fixture", command_sequence=1),
    )

    assert all(item.status == "pass" for item in result.criteria)
    assert controller.restored == (
        [f"checkpoint:{scenario}"] if "conflicting" in scenario else []
    )


def test_workflow1_negative_evaluator_refuses_failure_tax_without_operation_identity() -> None:
    execution = _execution()
    action = next(item for item in execution.actions if item.scenario == "negative-rate-limit")
    observation = BackendObservation(
        evidence=("events.ndjson#evidence",),
        run={
            "status": "failed",
            "failure_state": {"reason": "node_execution_failed"},
        },
        audits={
            "records": [
                {
                    "audit_id": "audit-leak",
                    "status": "failed",
                    "cost_usd": 0.001,
                    "cost_event_id": "cost-leak",
                    "execution_metadata": {"provider_request_id": "provider-leak"},
                }
            ]
        },
        cost={"run_cost_delta_usd": 0.001},
    )

    with pytest.raises(RuntimeError, match="operation identity"):
        Workflow1NegativeEvaluator().evaluate(action, observation)


def test_workflow1_stateful_negative_refuses_before_http_without_controller(
    tmp_path: Path,
) -> None:
    execution = _execution()
    action = next(
        item for item in execution.actions if item.scenario == "negative-conflicting-document"
    )
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    backend = _w1_negative_backend(execution, action, tmp_path, handler)
    with pytest.raises(RuntimeError, match="scenario controller"):
        backend.execute(
            action,
            ActionRecorder(EvidenceStore(tmp_path), step_id="conflict", command_sequence=1),
        )
    assert calls == 0
