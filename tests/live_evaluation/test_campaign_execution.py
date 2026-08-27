from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from release.live_evaluation.campaign_execution import (
    CampaignExecutionSettings,
    Workflow1Query,
    Workflow2BatchInput,
    Workflow3ActionPayload,
    build_campaign_execution,
)
from release.live_evaluation.coordinator import (
    ActionRecorder,
    CampaignCoordinator,
    CriterionResult,
    StepResult,
)
from release.live_evaluation.evidence import EvidenceStore
from release.live_evaluation.action_runner import EvaluationActionOutput, EvaluationActionPayload
from zeroth.contracts.graph.models import (
    AgentNode,
    EntrypointNode,
    ExecutableUnitNode,
    HumanApprovalNode,
    IfNode,
    LoopNode,
    RetrievalNode,
    SubgraphNode,
)
from zeroth.runtime.graph_validation import GraphValidator


def _settings() -> CampaignExecutionSettings:
    return CampaignExecutionSettings(
        campaign_id="evaluation-studio-v1",
        tenant_id="evaluation-studio-v1",
        model="openai/gpt-4o-mini",
        embedding_model="openai/text-embedding-3-small",
        chroma_connector_ref="eval_chroma_v1",
    )


class _RecordingBackend:
    def __init__(self) -> None:
        self.requests = []

    def execute(self, action, recorder: ActionRecorder) -> StepResult:
        self.requests.append(action.request)
        evidence = recorder.record_api_result(
            method="POST",
            path="/test-only/recorded-action",
            status_code=200,
            metadata={"scenario": action.scenario},
        )
        return StepResult(
            tuple(
                CriterionResult(criterion_id, "pass", (evidence,))
                for criterion_id in action.criterion_ids
            )
        )


def test_workflow1_is_grounded_bounded_and_instrumented() -> None:
    execution = build_campaign_execution(_settings())
    graph = execution.graphs.workflow1

    retrieval = next(node for node in graph.nodes if isinstance(node, RetrievalNode))
    researcher = next(node for node in graph.nodes if isinstance(node, AgentNode))
    revision_loop = next(node for node in graph.nodes if isinstance(node, LoopNode))
    repeat = next(edge for edge in graph.edges if edge.source_node_id == revision_loop.node_id)
    initialize = next(edge for edge in graph.edges if edge.target_node_id == revision_loop.node_id and edge.source_node_id == "request")
    loop_return = next(
        edge
        for edge in graph.edges
        if edge.source_node_id == researcher.node_id
        and edge.target_node_id == revision_loop.node_id
    )

    assert retrieval.retrieval.connector_ref == "eval_chroma_v1"
    assert retrieval.retrieval.top_k == 3
    assert retrieval.capability_bindings == ["memory_read"]
    assert researcher.agent.model_provider == "openai/gpt-4o-mini"
    assert revision_loop.loop.until == "payload.revision_required != True"
    assert revision_loop.loop.max_retries == 1
    assert revision_loop.input_contract_ref == "evaluation-studio-v1.workflow1.answer@1"
    assert revision_loop.output_contract_ref == "evaluation-studio-v1.workflow1.loop-outcome@1"
    assert initialize.mapping is not None
    assert initialize.mapping.model_dump(mode="json") == {
        "operations": [
            {"target_path": "query", "operation": "passthrough", "source_path": "query"},
            {"target_path": "answer", "operation": "constant", "value": ""},
            {"target_path": "source_ids", "operation": "constant", "value": []},
            {"target_path": "revision_required", "operation": "constant", "value": False},
            {"target_path": "revision_count", "operation": "constant", "value": 0},
        ]
    }
    assert repeat.mapping is not None
    assert repeat.mapping.model_dump(mode="json") == {
        "operations": [
            {"target_path": "query", "operation": "passthrough", "source_path": "query"},
        ]
    }
    assert graph.execution_settings.max_visits_per_node == 3
    assert graph.execution_settings.max_total_steps == 8
    assert repeat.condition is not None
    assert repeat.condition.allow_cycle_traversal is True
    assert repeat.condition.metadata == {"loop_route": "repeat"}
    assert repeat.metadata["source_handle"] == "repeat"
    assert loop_return.condition is not None
    assert loop_return.condition.allow_cycle_traversal is True
    assert not any(
        edge.source_node_id == researcher.node_id and edge.target_node_id == retrieval.node_id
        for edge in graph.edges
    )
    for node in (retrieval, researcher):
        assert node.execution_config["instrumentation"] == {
            "campaign_id": "${campaign_id}",
            "operation_id": "${operation_id}",
            "run_id": "${run_id}",
            "fail_closed": True,
        }


def test_workflow2_uses_canonical_child_deployment_and_bounded_batching() -> None:
    execution = build_campaign_execution(_settings())
    child = execution.graphs.workflow2_child
    parent = execution.graphs.workflow2_parent

    subgraph = next(node for node in parent.nodes if isinstance(node, SubgraphNode))
    fanout = next(node for node in parent.nodes if node.parallel_config is not None)
    retrieval = next(node for node in child.nodes if isinstance(node, RetrievalNode))
    request = next(node for node in child.nodes if isinstance(node, EntrypointNode))

    assert child.graph_id == "evaluation-studio-v1-batched-investigation-child"
    assert child.entry_step == "request"
    assert request.output_contract_ref == retrieval.input_contract_ref
    assert any(
        edge.source_node_id == request.node_id and edge.target_node_id == retrieval.node_id
        for edge in child.edges
    )
    assert retrieval.capability_bindings == ["memory_read"]
    assert subgraph.subgraph.graph_ref == execution.deployments.workflow2_child
    assert subgraph.subgraph.version == 2
    assert subgraph.subgraph.thread_participation == "isolated"
    assert subgraph.subgraph.max_depth == 2
    assert fanout.parallel_config is not None
    assert fanout.parallel_config.split_path == "items"
    assert fanout.parallel_config.max_branches == 24
    assert fanout.parallel_config.batch_size == 4
    assert fanout.parallel_config.max_concurrency == 4
    assert fanout.parallel_config.merge_strategy == "collect"
    synthesize = next(
        node
        for node in parent.nodes
        if isinstance(node, AgentNode) and node.node_id == "synthesize"
    )
    assert synthesize.join_config is not None
    assert synthesize.join_config.merge_strategy == "collect"
    assert synthesize.join_config.merge_path == "results"
    assert parent.metadata["preserve_input_index_order"] is True
    assert parent.metadata["batch_validation"] == "tenant_contract"
    pause = next(
        node
        for node in child.nodes
        if isinstance(node, HumanApprovalNode) and node.node_id == "evaluation-child-pause"
    )
    failure = next(
        node
        for node in child.nodes
        if isinstance(node, ExecutableUnitNode) and node.node_id == "evaluation-child-failure"
    )
    assert pause.human_approval.sla_timeout_seconds == 300
    assert failure.executable_unit.manifest_ref == "evaluation://controlled-failure/v1"
    conditioned = {
        edge.target_node_id: edge.condition.expression
        for edge in child.edges
        if edge.condition is not None
    }
    assert conditioned == {
        "investigate": "payload.evaluation_behavior is None",
        "evaluation-child-pause": "payload.evaluation_behavior == 'child_pause'",
        "evaluation-child-failure": "payload.evaluation_behavior == 'child_failure'",
    }
    deployment_gate = next(
        action
        for action in execution.actions
        if action.workflow == "workflow2" and action.action_type == "deployment_gate"
    )
    assert [graph.graph_id for graph in deployment_gate.graph_specs] == [
        child.graph_id,
        parent.graph_id,
    ]


def test_campaign_conditions_only_reference_runtime_namespaces() -> None:
    execution = build_campaign_execution(_settings())

    for graph in (
        execution.graphs.workflow1,
        execution.graphs.workflow2_child,
        execution.graphs.workflow2_parent,
        execution.graphs.workflow3,
    ):
        for edge in graph.edges:
            if edge.condition is None:
                continue
            assert all(
                operand_ref.split(".", 1)[0]
                in {
                    "payload",
                    "state",
                    "variables",
                    "node_visit_counts",
                    "edge_visit_counts",
                    "path",
                    "metadata",
                }
                for operand_ref in edge.condition.operand_refs
            )
def test_batch_and_action_contracts_reject_invalid_inputs_without_a_model_call() -> None:
    with pytest.raises(ValidationError):
        Workflow2BatchInput(items=[])
    with pytest.raises(ValidationError):
        Workflow2BatchInput(items=[{"index": index, "query": f"q-{index}"} for index in range(25)])
    assert (
        Workflow3ActionPayload(ticket="synthetic-ticket-1", status="remediated").status
        == "remediated"
    )
    with pytest.raises(ValidationError):
        Workflow3ActionPayload(ticket_id="ticket-1", requested_status="remediated")


def test_workflow3_contracts_match_the_evaluation_action_runner_exactly() -> None:
    from release.live_evaluation.campaign_execution import Workflow3ActionReceipt

    assert Workflow3ActionPayload.model_fields.keys() == EvaluationActionPayload.model_fields.keys()
    assert Workflow3ActionReceipt.model_fields.keys() == EvaluationActionOutput.model_fields.keys()
    assert (
        Workflow3ActionPayload.model_json_schema()["properties"]["ticket"]["pattern"]
        == EvaluationActionPayload.model_json_schema()["properties"]["ticket"]["pattern"]
    )


def test_execution_bundle_exposes_every_tenant_contract_needed_for_publish() -> None:
    execution = build_campaign_execution(_settings())
    refs = {contract.ref for contract in execution.contracts}

    for graph in (
        execution.graphs.workflow1,
        execution.graphs.workflow2_child,
        execution.graphs.workflow2_parent,
        execution.graphs.workflow3,
    ):
        for node in graph.nodes:
            assert node.input_contract_ref in refs
            assert node.output_contract_ref in refs
            if isinstance(node, HumanApprovalNode):
                assert node.human_approval.approval_payload_schema_ref in refs
                assert node.human_approval.resolution_schema_ref in refs


def test_workflow3_uses_approval_and_only_the_local_evaluation_action() -> None:
    execution = build_campaign_execution(_settings())
    graph = execution.graphs.workflow3

    approval = next(node for node in graph.nodes if isinstance(node, HumanApprovalNode))
    action = next(node for node in graph.nodes if isinstance(node, ExecutableUnitNode))

    # Human-operated console approval must leave enough time for page hydration,
    # assistive-technology navigation, and a deliberate reviewer decision.
    assert approval.human_approval.sla_timeout_seconds == 60
    assert approval.human_approval.escalation_action == "auto_reject"
    assert approval.human_approval.approval_policy_config["require_explicit_decision"] is True
    assert action.executable_unit.manifest_ref == "evaluation://synthetic-action/v1"
    assert action.executable_unit.execution_mode == "native"
    route = next(node for node in graph.nodes if isinstance(node, IfNode))
    assert route.node_id == "evaluation-route"
    assert route.display.title == "Route remediation"
    assert route.condition.expression == (
        "payload.evaluation_behavior == 'cancel_after_approval'"
    )
    assert [edge.edge_id for edge in graph.edges] == [
        "request-approval",
        "approval-evaluation-route",
        "evaluation-route-action",
        "evaluation-route-barrier",
        "evaluation-barrier-action",
    ]
    route_edges = [edge for edge in graph.edges if edge.source_node_id == route.node_id]
    assert {edge.metadata["source_handle"] for edge in route_edges} == {"true", "false"}
    assert {edge.condition.metadata["if_route"] for edge in route_edges} == {
        "true",
        "false",
    }
    barrier = next(
        node
        for node in graph.nodes
        if isinstance(node, HumanApprovalNode) and node.node_id == "evaluation-pre-action-barrier"
    )
    assert barrier.human_approval.sla_timeout_seconds == 300
    cancellation = next(
        item
        for item in execution.actions
        if item.scenario == "negative-cancellation-after-approval"
    )
    assert cancellation.request.body["input_payload"]["evaluation_behavior"] == (
        "cancel_after_approval"
    )
    happy = next(
        item
        for item in execution.actions
        if item.scenario == "happy-1" and item.workflow == "workflow3"
    )
    assert happy.request.input_payload == {
        "ticket": "synthetic-ticket-1",
        "status": "remediated",
    }


async def test_all_graphs_pass_product_publish_time_structure_validation() -> None:
    graphs = build_campaign_execution(_settings()).graphs
    validator = GraphValidator()

    for graph in (
        graphs.workflow1,
        graphs.workflow2_child,
        graphs.workflow2_parent,
        graphs.workflow3,
    ):
        report = await validator.validate(graph)
        assert not report.errors, [(issue.code, issue.message) for issue in report.issues]


def test_every_action_has_complete_unique_identity_and_three_happy_repetitions() -> None:
    execution = build_campaign_execution(_settings())

    identities = [action.request.identity for action in execution.actions]
    assert all(identity.campaign_id == "evaluation-studio-v1" for identity in identities)
    assert all(identity.operation_id and identity.run_id for identity in identities)
    assert len({identity.operation_id for identity in identities}) == len(identities)
    assert len({identity.run_id for identity in identities}) == len(identities)
    for workflow in ("workflow1", "workflow2", "workflow3"):
        happy = [
            action
            for action in execution.actions
            if action.workflow == workflow and action.scenario.startswith("happy-")
        ]
        assert [action.scenario for action in happy] == ["happy-1", "happy-2", "happy-3"]
        assert all(action.request.body["campaign_id"] for action in happy)
        assert all(action.request.body["input_payload"] for action in happy)
        assert all(action.request.body["campaign_strict"] is True for action in happy)
        assert all("operation_id" not in action.request.body for action in happy)
        assert all("run_id" not in action.request.body for action in happy)
        assert all(action.request.correlation_expectations["operation_id"] for action in happy)
        assert all(action.request.correlation_expectations["run_id"] for action in happy)


def test_every_negative_case_has_a_deterministic_fault_or_input_plan() -> None:
    execution = build_campaign_execution(_settings())
    negatives = [action for action in execution.actions if action.scenario.startswith("negative-")]

    assert negatives
    assert all(action.request.fault is not None for action in negatives)
    assert all(action.request.fault_control is not None for action in negatives)
    assert all("fault_injection" not in action.request.body for action in negatives)
    assert all(action.request.fault.deterministic for action in negatives if action.request.fault)
    by_scenario = {action.scenario: action.request.fault for action in negatives}
    assert by_scenario["negative-rate-limit"].mode == "rate_limit"
    assert by_scenario["negative-malformed-response"].mode == "malformed_response"
    assert by_scenario["negative-timeout-after-commit"].mode == "timeout_after_commit"
    assert by_scenario["negative-sink-unavailable"].target == "action_sink"
    ambiguous = by_scenario["negative-ambiguous-no-reexecution"]
    assert ambiguous.target == "action_outcome_lookup"
    assert ambiguous.mode == "unavailable"


def test_negative_payloads_reach_the_intended_boundary_or_fail_only_the_input_contract() -> None:
    execution = build_campaign_execution(_settings())
    actions = {action.scenario: action for action in execution.actions}

    Workflow1Query.model_validate(actions["negative-rate-limit"].request.input_payload)
    Workflow1Query.model_validate(actions["negative-chroma-unavailable"].request.input_payload)
    with pytest.raises(ValidationError):
        Workflow1Query.model_validate(actions["negative-empty-query"].request.input_payload)
    with pytest.raises(ValidationError):
        Workflow1Query.model_validate(actions["negative-oversized-query"].request.input_payload)

    Workflow2BatchInput.model_validate(actions["negative-cancellation"].request.input_payload)
    assert len(actions["negative-cancellation"].request.input_payload["items"]) == 8
    paused = actions["negative-child-pause-partial-collection"].request.input_payload["items"]
    failed = actions["negative-child-failure-partial-collection"].request.input_payload["items"]
    assert paused[3]["evaluation_behavior"] == "child_pause"
    assert failed[3]["evaluation_behavior"] == "child_failure"
    assert all("evaluation_behavior" not in item for index, item in enumerate(paused) if index != 3)
    with pytest.raises(ValidationError):
        Workflow2BatchInput.model_validate(actions["negative-empty-batch"].request.input_payload)
    with pytest.raises(ValidationError):
        Workflow2BatchInput.model_validate(actions["negative-over-24-batch"].request.input_payload)
    with pytest.raises(ValidationError):
        Workflow2BatchInput.model_validate(actions["negative-malformed-item"].request.input_payload)

    timeout_payload = Workflow3ActionPayload.model_validate(
        actions["negative-timeout-after-commit"].request.input_payload
    )
    assert timeout_payload.fault == "timeout_after_commit"
    ambiguous_payload = Workflow3ActionPayload.model_validate(
        actions["negative-ambiguous-no-reexecution"].request.input_payload
    )
    assert ambiguous_payload.fault == "timeout_after_commit"
    rejection = Workflow3ActionPayload.model_validate(
        actions["negative-rejection-zero-marker"].request.input_payload
    )
    assert rejection.fault is None


def test_unconfigured_execution_fails_closed_without_calling_any_live_system(
    tmp_path: Path,
) -> None:
    execution = build_campaign_execution(_settings())

    summary = CampaignCoordinator(EvidenceStore(tmp_path), execution.plan).run()

    assert not summary.completed
    assert summary.halted_by == "workflow1.health-exact-graph-version"
    events = (tmp_path / "events.ndjson").read_text()
    assert '"exception_type":"RuntimeError"' in events
    assert "campaign.api.completed" not in events


def test_recording_backend_drives_pluggable_actions_without_network(tmp_path: Path) -> None:
    backend = _RecordingBackend()
    execution = build_campaign_execution(_settings(), backend=backend)

    summary = CampaignCoordinator(EvidenceStore(tmp_path), execution.plan).run()

    assert summary.completed
    assert len(backend.requests) == len(execution.actions)
    assert ":health:" in backend.requests[0].identity.operation_id
    assert all(
        request.identity.campaign_id == _settings().campaign_id for request in backend.requests
    )
