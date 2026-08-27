from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from release.live_evaluation.campaign_execution import (
    CampaignExecutionSettings,
    WorkflowAction,
    build_campaign_execution,
)
from release.live_evaluation.campaign_http import (
    BackendObservation,
    HttpBackendConfig,
    HttpCampaignExecutionBackend,
    PublishedGraph,
    provider_acknowledgement,
)
from release.live_evaluation.coordinator import ActionRecorder
from release.live_evaluation.evidence import EvidenceStore
from release.live_evaluation.workflow2_scenarios import (
    LocalWorkflow2ScenarioController,
    PreparedWorkflow2Scenario,
    UnsupportedWorkflow2ScenarioError,
    Workflow2NegativeEvaluator,
)


def _action(scenario: str) -> WorkflowAction:
    execution = build_campaign_execution(
        CampaignExecutionSettings(
            campaign_id="evaluation-studio-v1",
            tenant_id="evaluation-studio-v1",
            model="openai/gpt-4o-mini",
            embedding_model="openai/text-embedding-3-small",
            chroma_connector_ref="eval_chroma_v1",
        )
    )
    return next(
        item
        for item in execution.actions
        if item.workflow == "workflow2" and item.scenario == scenario
    )


def _recorder(tmp_path: Path) -> ActionRecorder:
    return ActionRecorder(EvidenceStore(tmp_path), step_id="workflow2-negative", command_sequence=1)


def _runtime_observation(
    *,
    status: str = "succeeded",
    results: list[dict[str, object]] | None = None,
    failure_reason: str | None = None,
    children: int = 8,
) -> BackendObservation:
    output_results = results or [
        {"index": index, "answer": f"answer-{index}", "source_ids": [f"doc-{index}"], "error": None}
        for index in range(8)
    ]
    records = []
    for index in range(children):
        records.append(
            {
                "audit_id": f"audit-{index}",
                "node_id": f"branch:{index}:subgraph:child:1:investigate",
                "status": "completed",
                "cost_usd": 0.001,
                "cost_event_id": f"cost-{index}",
                "execution_metadata": {
                    "branch_index": index,
                    "subgraph_run_id": f"child-{index}",
                    "operation_id": f"operation-{index}",
                    "provider_request_id": f"provider-{index}",
                },
            }
        )
    run: dict[str, object] = {
        "run_id": "parent-run",
        "status": status,
        "terminal_output": {"results": output_results},
    }
    if failure_reason is not None:
        run["failure_state"] = {"reason": failure_reason, "message": failure_reason}
    child_runs = tuple(
        {
            "run_id": f"child-{index}",
            "thread_id": f"thread-{index}",
            "status": "succeeded",
            "tenant_id": "evaluation-studio-v1",
        }
        for index in range(children)
    )
    return BackendObservation(
        evidence=("events.ndjson#event-1",),
        run=run,
        audits={"records": records},
        cost={"run_cost_delta_usd": children * 0.001},
        children=child_runs,
    )


@pytest.mark.parametrize(
    ("scenario", "issue_type"),
    [
        ("negative-empty-batch", "too_short"),
        ("negative-over-24-batch", "too_long"),
        ("negative-malformed-item", "missing"),
    ],
)
def test_workflow2_input_negatives_require_422_without_runtime_effects(
    scenario: str, issue_type: str
) -> None:
    action = _action(scenario)
    observation = BackendObservation(
        evidence=("events.ndjson#rejection",),
        submission={"status_code": 422, "issue_types": [issue_type]},
        cost={"run_cost_delta_usd": 0.0},
    )

    result = Workflow2NegativeEvaluator().evaluate(action, observation)

    assert result.criteria[0].status == "pass"


def test_retrieval_miss_requires_seven_successes_and_one_explicit_error() -> None:
    action = _action("negative-retrieval-miss")
    results = [
        {
            "index": index,
            "answer": None if index == 3 else f"answer-{index}",
            "source_ids": [] if index == 3 else [f"doc-{index}"],
            "error": "retrieval_miss" if index == 3 else None,
        }
        for index in range(8)
    ]

    result = Workflow2NegativeEvaluator().evaluate(action, _runtime_observation(results=results))

    assert result.criteria[0].status == "pass"


def test_retrieval_miss_refuses_silent_success_for_all_eight_items() -> None:
    with pytest.raises(RuntimeError, match="seven successful"):
        Workflow2NegativeEvaluator().evaluate(
            _action("negative-retrieval-miss"), _runtime_observation()
        )


def test_cancellation_requires_operator_cancelled_parent_and_no_duplicate_identity() -> None:
    observation = _runtime_observation(
        status="failed", failure_reason="operator_cancelled", children=2
    )

    result = Workflow2NegativeEvaluator().evaluate(_action("negative-cancellation"), observation)

    assert result.criteria[0].status == "pass"

    duplicate = dict(observation.audits or {})
    duplicate_records = list(duplicate["records"])
    duplicate_records[1] = {
        **duplicate_records[1],
        "execution_metadata": {
            **duplicate_records[1]["execution_metadata"],
            "operation_id": "operation-0",
        },
    }
    with pytest.raises(RuntimeError, match="reexecution"):
        Workflow2NegativeEvaluator().evaluate(
            _action("negative-cancellation"),
            BackendObservation(
                evidence=observation.evidence,
                run=observation.run,
                audits={"records": duplicate_records},
                cost=observation.cost,
                children=observation.children,
            ),
        )

    incomplete = _runtime_observation(
        status="failed", failure_reason="operator_cancelled", children=1
    )
    with pytest.raises(RuntimeError, match="two completed child"):
        Workflow2NegativeEvaluator().evaluate(_action("negative-cancellation"), incomplete)


def test_refresh_restoration_requires_durable_same_run_ui_checkpoint() -> None:
    observation = _runtime_observation()
    observation = BackendObservation(
        evidence=observation.evidence,
        run=observation.run,
        audits=observation.audits,
        cost=observation.cost,
        children=observation.children,
        submission={
            "before_refresh_run_id": "parent-run",
            "restored_run_id": "parent-run",
            "keyboard_restoration_passed": True,
            "ui_evidence": "screenshots/workflow2-refresh-restored.png",
        },
    )

    result = Workflow2NegativeEvaluator().evaluate(
        _action("negative-refresh-restoration"), observation
    )

    assert result.criteria[0].status == "pass"


@pytest.mark.parametrize(
    ("before", "restored"),
    [("different-before", "parent-run"), ("parent-run", "different-after")],
)
def test_refresh_restoration_rejects_relabelled_or_changed_run_identity(
    before: str, restored: str
) -> None:
    observation = _runtime_observation()
    observation = BackendObservation(
        evidence=observation.evidence,
        run=observation.run,
        audits=observation.audits,
        cost=observation.cost,
        children=observation.children,
        submission={
            "before_refresh_run_id": before,
            "restored_run_id": restored,
            "keyboard_restoration_passed": True,
            "ui_evidence": "screenshots/workflow2-refresh-restored.png",
        },
    )

    with pytest.raises(RuntimeError, match="same run"):
        Workflow2NegativeEvaluator().evaluate(
            _action("negative-refresh-restoration"), observation
        )


@pytest.mark.parametrize(
    ("scenario", "status", "paused"),
    [
        ("negative-child-pause-partial-collection", "paused_for_approval", True),
        ("negative-child-failure-partial-collection", "succeeded", False),
    ],
)
def test_child_pause_and_failure_require_explicit_ordered_partial_collection(
    scenario: str, status: str, paused: bool
) -> None:
    results = [
        {
            "index": index,
            "answer": None if index == 3 else f"answer-{index}",
            "source_ids": [] if index == 3 else [f"doc-{index}"],
            "error": ("child_paused" if paused else "child_failed") if index == 3 else None,
        }
        for index in range(8)
    ]
    observation = _runtime_observation(status=status, results=results, children=8)
    run = dict(observation.run or {})
    if paused:
        run["approval_paused_state"] = {
            "approval_id": "approval-child-3",
            "node_id": "branch:3:subgraph:child:1:evaluation-child-pause",
        }
        run.pop("terminal_output", None)
        children = tuple(
            {
                **child,
                "terminal_output": {
                    "index": index,
                    "answer": f"answer-{index}",
                    "source_ids": [f"doc-{index}"],
                    "error": None,
                },
            }
            for index, child in enumerate(observation.children)
            if index != 3
        )
    else:
        children = observation.children
    observation = BackendObservation(
        evidence=observation.evidence,
        run=run,
        audits=observation.audits,
        cost=observation.cost,
        children=children,
    )

    result = Workflow2NegativeEvaluator().evaluate(_action(scenario), observation)

    assert result.criteria[0].status == "pass"


def test_runtime_checks_require_ordering_isolation_and_cost_reconciliation() -> None:
    results = [
        {
            "index": index,
            "answer": None if index == 3 else f"answer-{index}",
            "source_ids": [] if index == 3 else [f"doc-{index}"],
            "error": "retrieval_miss" if index == 3 else None,
        }
        for index in range(8)
    ]
    observation = _runtime_observation(results=results)
    reversed_output = dict(observation.run or {})
    reversed_output["terminal_output"] = {
        "results": list(reversed(reversed_output["terminal_output"]["results"]))
    }
    with pytest.raises(RuntimeError, match="index ordered"):
        Workflow2NegativeEvaluator().evaluate(
            _action("negative-retrieval-miss"),
            BackendObservation(
                evidence=observation.evidence,
                run=reversed_output,
                audits=observation.audits,
                cost=observation.cost,
                children=observation.children,
            ),
        )

    duplicate_threads = list(observation.children)
    duplicate_threads[1] = {**duplicate_threads[1], "thread_id": "thread-0"}
    with pytest.raises(RuntimeError, match="isolated"):
        Workflow2NegativeEvaluator().evaluate(
            _action("negative-retrieval-miss"),
            BackendObservation(
                evidence=observation.evidence,
                run=observation.run,
                audits=observation.audits,
                cost=observation.cost,
                children=tuple(duplicate_threads),
            ),
        )

    with pytest.raises(RuntimeError, match="reconcile"):
        Workflow2NegativeEvaluator().evaluate(
            _action("negative-retrieval-miss"),
            BackendObservation(
                evidence=observation.evidence,
                run=observation.run,
                audits=observation.audits,
                cost={"run_cost_delta_usd": 2.0},
                children=observation.children,
            ),
        )


def test_local_controller_fails_closed_until_runtime_and_ui_have_deterministic_boundaries(
    tmp_path: Path,
) -> None:
    controller = LocalWorkflow2ScenarioController()
    for scenario in (
        "negative-cancellation",
        "negative-refresh-restoration",
        "negative-child-pause-partial-collection",
        "negative-child-failure-partial-collection",
    ):
        with pytest.raises(UnsupportedWorkflow2ScenarioError, match="fail closed"):
            controller.prepare(_action(scenario), _recorder(tmp_path))


def test_local_controller_binds_and_cancels_exact_remote_run(tmp_path: Path) -> None:
    controller_paths: list[str] = []

    def controller_handler(request: httpx.Request) -> httpx.Response:
        controller_paths.append(request.url.path)
        if request.url.path == "/v1/scenarios/prepare":
            return httpx.Response(
                200,
                json={
                    "fixture_id": "fixture-cancel",
                    "evidence": ["events.ndjson#prepared"],
                },
            )
        return httpx.Response(
            200,
            json={"run_id": "parent-run", "evidence": "events.ndjson#bound"},
        )

    controller = LocalWorkflow2ScenarioController(
        controller_url="http://127.0.0.1:8199",
        controller_key="controller-key",
        workflow_id="evaluation-studio-v1-batched-investigation-parent-v1",
        client=httpx.Client(transport=httpx.MockTransport(controller_handler)),
    )
    recorder = _recorder(tmp_path)
    action = _action("negative-cancellation")
    prepared = controller.prepare(action, recorder)

    evidence = controller.after_submission(
        action,
        prepared,
        run_id="parent-run",
        base_url="http://127.0.0.1:8103",
        request=lambda *args, **kwargs: httpx.Response(
            200, json={"run_id": "parent-run", "status": "failed"}
        ),
        recorder=recorder,
    )

    assert controller_paths == [
        "/v1/scenarios/prepare",
        "/v1/scenarios/fixture-cancel/checkpoints/run_submitted",
    ]
    assert "events.ndjson#bound" in evidence


class _AuthoritativeCancellationController:
    def prepare(
        self, action: WorkflowAction, recorder: ActionRecorder
    ) -> PreparedWorkflow2Scenario:
        evidence = recorder.record_ui_action(
            action="prepare-runtime-child-index-barrier",
            outcome="external controller ready",
            metadata={"required_completed_children": 2},
        )
        return PreparedWorkflow2Scenario(
            checkpoint_id="external-child-barrier",
            post_submission_action="cancel",
            evidence=(evidence,),
        )

    def after_submission(
        self,
        action: WorkflowAction,
        prepared: PreparedWorkflow2Scenario,
        *,
        run_id: str,
        base_url: str,
        request,
        recorder: ActionRecorder,
    ) -> tuple[str, ...]:
        response = request("POST", f"{base_url}/admin/runs/{run_id}/cancel", expected={200})
        return (
            recorder.record_api_result(
                method="POST",
                path=f"/admin/runs/{run_id}/cancel",
                status_code=response.status_code,
                metadata={"completed_children_at_barrier": 2},
            ),
        )


class _Publisher:
    def publish(self, *, graphs, contracts, tenant_id, workspace_id):
        return tuple(PublishedGraph(graph.graph_id, 1) for graph in graphs)


def _backend(
    action: WorkflowAction,
    handler,
    *,
    controller: LocalWorkflow2ScenarioController | None = None,
) -> HttpCampaignExecutionBackend:
    execution = build_campaign_execution(
        CampaignExecutionSettings(
            campaign_id="evaluation-studio-v1",
            tenant_id="evaluation-studio-v1",
            model="openai/gpt-4o-mini",
            embedding_model="openai/text-embedding-3-small",
            chroma_connector_ref="eval_chroma_v1",
        )
    )
    return HttpCampaignExecutionBackend(
        config=HttpBackendConfig(
            console_base_url="http://localhost:8000",
            deployment_base_urls={
                execution.deployments.workflow2_parent: "http://localhost:8002",
                execution.deployments.workflow2_child: "http://localhost:8003",
            },
            local_fault_control_url="http://localhost:8004",
            campaign_id=execution.settings.campaign_id,
            provider_execution_enabled=True,
            provider_acknowledgement=provider_acknowledgement(execution.settings.campaign_id),
            poll_interval_seconds=0,
            max_poll_attempts=2,
        ),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        publisher=_Publisher(),
        evaluator=Workflow2NegativeEvaluator(),
        contracts=execution.contracts,
        tenant_id=execution.settings.tenant_id,
        workflow2_scenario_controller=controller,
    )


def test_http_backend_rejects_invalid_batch_without_arming_fault_or_creating_run(
    tmp_path: Path,
) -> None:
    action = _action("negative-empty-batch")
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/cost"):
            return httpx.Response(200, json={"total_cost_usd": 1.25})
        if request.url.path == "/v1/runs":
            return httpx.Response(
                422,
                json={"detail": [{"type": "too_short", "loc": ["items"]}]},
            )
        raise AssertionError(request.url)

    result = _backend(action, handler).execute(action, _recorder(tmp_path))

    assert result.criteria[0].status == "pass"
    assert "/faults/arm" not in paths
    assert paths.count("/v1/runs") == 1


def test_http_backend_arms_retrieval_miss_and_collects_authoritative_child_runs(
    tmp_path: Path,
) -> None:
    action = _action("negative-retrieval-miss")
    paths: list[str] = []
    child_ref = "evaluation-studio-v1-batched-investigation-child-v1"
    results = [
        {
            "index": index,
            "answer": None if index == 3 else f"answer-{index}",
            "source_ids": [] if index == 3 else [f"doc-{index}"],
            "error": "retrieval_miss" if index == 3 else None,
        }
        for index in range(8)
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/faults/arm":
            return httpx.Response(204)
        if request.url.path.endswith("/cost"):
            total = 2.009 if paths.count(request.url.path) > 1 else 2.0
            return httpx.Response(200, json={"total_cost_usd": total})
        if request.url.path == "/v1/runs" and request.url.port == 8002:
            return httpx.Response(202, json={"run_id": "parent-run"})
        if request.url.path == "/v1/runs/parent-run":
            return httpx.Response(
                200,
                json={
                    "run_id": "parent-run",
                    "status": "succeeded",
                    "terminal_output": {"results": results},
                },
            )
        if request.url.port == 8003 and request.url.path.endswith("/audits"):
            child_index = request.url.params["run_id"].rsplit("-", 1)[1]
            return httpx.Response(
                200,
                json={
                    "records": [
                        {
                            "audit_id": f"child-audit-{child_index}",
                            "node_id": "investigate",
                            "status": "completed",
                            "cost_usd": 0.001,
                            "cost_event_id": f"child-cost-{child_index}",
                            "execution_metadata": {
                                "operation_id": f"child-operation-{child_index}",
                                "provider_request_id": f"child-provider-{child_index}",
                            },
                        }
                    ]
                },
            )
        if request.url.path.endswith("/audits"):
            return httpx.Response(
                200,
                json={
                    "records": [
                        {
                            "audit_id": f"audit-{index}",
                            "node_id": f"branch:{index}:subgraph:{child_ref}:1:investigate",
                            "status": "completed",
                            "cost_usd": 0.001,
                            "execution_metadata": {
                                "branch_index": index,
                                "subgraph_run_id": f"child-{index}",
                                "subgraph_graph_ref": child_ref,
                            },
                        }
                        for index in range(8)
                    ]
                    + [
                        {
                            "audit_id": "parent-synthesize",
                            "node_id": "synthesize",
                            "status": "completed",
                            "cost_usd": 0.001,
                            "cost_event_id": "parent-cost-synthesize",
                            "execution_metadata": {
                                "operation_id": "parent-operation-synthesize",
                                "provider_request_id": "parent-provider-synthesize",
                            },
                        }
                    ]
                },
            )
        if request.url.port == 8003 and request.url.path.startswith("/v1/runs/child-"):
            index = request.url.path.rsplit("-", 1)[1]
            return httpx.Response(
                200,
                json={
                    "run_id": f"child-{index}",
                    "thread_id": f"thread-{index}",
                    "status": "succeeded",
                    "tenant_id": "evaluation-studio-v1",
                },
            )
        raise AssertionError(request.url)

    result = _backend(action, handler).execute(action, _recorder(tmp_path))

    assert result.criteria[0].status == "pass"
    assert paths[1] == "/faults/arm"
    assert sum(path.startswith("/v1/runs/child-") for path in paths) == 8
    assert sum(path.endswith("/audits") for path in paths) == 9


def test_http_backend_cancels_submitted_parent_through_authorized_admin_route(
    tmp_path: Path,
) -> None:
    action = _action("negative-cancellation")
    paths: list[str] = []
    child_ref = "evaluation-studio-v1-batched-investigation-child-v1"

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/cost"):
            total = 4.002 if paths.count(request.url.path) > 1 else 4.0
            return httpx.Response(200, json={"total_cost_usd": total})
        if request.url.path == "/v1/runs":
            return httpx.Response(202, json={"run_id": "parent-run"})
        if request.url.path == "/admin/runs/parent-run/cancel":
            return httpx.Response(200, json={"run_id": "parent-run", "status": "failed"})
        if request.url.path == "/v1/runs/parent-run":
            return httpx.Response(
                200,
                json={
                    "run_id": "parent-run",
                    "status": "failed",
                    "failure_state": {"reason": "operator_cancelled", "message": "cancelled"},
                },
            )
        if request.url.port == 8003 and request.url.path.endswith("/audits"):
            child_index = request.url.params["run_id"].rsplit("-", 1)[1]
            return httpx.Response(
                200,
                json={
                    "records": [
                        {
                            "audit_id": f"child-audit-{child_index}",
                            "node_id": "investigate",
                            "status": "completed",
                            "cost_usd": 0.001,
                            "cost_event_id": f"child-cost-{child_index}",
                            "execution_metadata": {
                                "operation_id": f"child-operation-{child_index}",
                                "provider_request_id": f"child-provider-{child_index}",
                            },
                        }
                    ]
                },
            )
        if request.url.path.endswith("/audits"):
            return httpx.Response(
                200,
                json={
                    "records": [
                        {
                            "audit_id": f"audit-{index}",
                            "node_id": f"branch:{index}:subgraph:{child_ref}:1:investigate",
                            "status": "completed",
                            "cost_usd": 0.001,
                            "execution_metadata": {
                                "branch_index": index,
                                "subgraph_run_id": f"child-{index}",
                                "subgraph_graph_ref": child_ref,
                            },
                        }
                        for index in range(2)
                    ]
                },
            )
        if request.url.port == 8003 and request.url.path.startswith("/v1/runs/child-"):
            index = request.url.path.rsplit("-", 1)[1]
            return httpx.Response(
                200,
                json={
                    "run_id": f"child-{index}",
                    "thread_id": f"thread-{index}",
                    "status": "succeeded",
                },
            )
        raise AssertionError(request.url)

    result = _backend(action, handler, controller=_AuthoritativeCancellationController()).execute(
        action, _recorder(tmp_path)
    )

    assert result.criteria[0].status == "pass"
    assert paths.index("/admin/runs/parent-run/cancel") < paths.index("/v1/runs/parent-run")
