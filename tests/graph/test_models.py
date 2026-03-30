from __future__ import annotations

from governai.app.spec import GovernedFlowSpec

from zeroth.graph.models import (
    AgentNode,
    AgentNodeData,
    Condition,
    DisplayMetadata,
    Edge,
    ExecutableUnitNode,
    ExecutableUnitNodeData,
    ExecutionSettings,
    Graph,
    GraphStatus,
    HumanApprovalNode,
    HumanApprovalNodeData,
)
from zeroth.graph.serialization import deserialize_graph, serialize_graph
from zeroth.mappings.models import (
    ConstantMappingOperation,
    DefaultMappingOperation,
    EdgeMapping,
    PassthroughMappingOperation,
    RenameMappingOperation,
)


def build_graph() -> Graph:
    return Graph(
        graph_id="graph-1",
        name="Governed Demo",
        version=1,
        status=GraphStatus.DRAFT,
        entry_step="agent-step",
        policy_bindings=["policy://safety"],
        deployment_settings={"environment": "test"},
        metadata={"owner": "team-a"},
        execution_settings=ExecutionSettings(max_total_steps=10, max_visits_per_node=2),
        nodes=[
            AgentNode(
                node_id="agent-step",
                graph_version_ref="graph-1@1",
                display=DisplayMetadata(title="Agent"),
                input_contract_ref="contract://input",
                output_contract_ref="contract://output",
                agent=AgentNodeData(
                    instruction="Analyze the request",
                    model_provider="governai:model-router",
                    tool_refs=["tool://summarizer"],
                    memory_refs=["memory://run"],
                    retry_policy={"max_retries": 2},
                    state_persistence={"mode": "thread"},
                    thread_participation="full",
                ),
            ),
            ExecutableUnitNode(
                node_id="tool-step",
                graph_version_ref="graph-1@1",
                display=DisplayMetadata(title="Tool"),
                executable_unit=ExecutableUnitNodeData(
                    manifest_ref="eu://summarizer",
                    execution_mode="wrapped_command",
                    runtime_binding="python",
                    sandbox_config={"network": "off"},
                    output_extraction_strategy="json_stdout",
                ),
            ),
            HumanApprovalNode(
                node_id="approval-step",
                graph_version_ref="graph-1@1",
                display=DisplayMetadata(title="Approval"),
                human_approval=HumanApprovalNodeData(
                    approval_payload_schema_ref="schema://approval",
                    resolution_schema_ref="schema://resolution",
                    approval_policy_config={"requires_rationale": True},
                    pause_behavior_config={"resume_mode": "async"},
                ),
            ),
        ],
        edges=[
            Edge(
                edge_id="edge-1",
                source_node_id="agent-step",
                target_node_id="tool-step",
                mapping=EdgeMapping(
                    operations=[
                        PassthroughMappingOperation(
                            source_path="payload.user.name",
                            target_path="request.user.name",
                        ),
                        RenameMappingOperation(
                            source_path="payload.user.id",
                            target_path="request.user.identifier",
                        ),
                        ConstantMappingOperation(
                            target_path="request.source",
                            value="zeroth",
                        ),
                        DefaultMappingOperation(
                            source_path="payload.user.locale",
                            target_path="request.user.locale",
                            default_value="en-US",
                        ),
                    ]
                ),
                condition=Condition(
                    expression="payload.user.id is not None",
                    operand_refs=["payload.user.id"],
                ),
            ),
            Edge(
                edge_id="edge-2",
                source_node_id="tool-step",
                target_node_id="approval-step",
            ),
        ],
    )


def test_graph_serialization_round_trip_preserves_governai_shape() -> None:
    graph = build_graph()

    encoded = serialize_graph(graph)
    decoded = deserialize_graph(encoded)

    assert decoded == graph
    assert isinstance(decoded.to_governed_flow_spec(), GovernedFlowSpec)
    assert decoded.nodes[0].agent.tool_refs == ["tool://summarizer"]
    assert decoded.edges[0].mapping is not None


def test_graph_compiles_to_governai_flow_spec() -> None:
    spec = build_graph().to_governed_flow_spec()

    assert isinstance(spec, GovernedFlowSpec)
    assert spec.name == "Governed Demo"
    assert spec.entry_step == "agent-step"
    assert spec.policies == [{"ref": "policy://safety"}]
    assert spec.steps[0].agent["kind"] == "agent_ref"
    assert spec.steps[0].transition.kind == "then"
    assert spec.steps[1].tool["kind"] == "executable_unit_ref"
    assert spec.steps[1].transition.kind == "then"
    assert spec.steps[2].agent["kind"] == "human_approval_ref"
    assert spec.steps[2].transition.kind == "end"


def test_graph_lifecycle_transitions() -> None:
    graph = build_graph()

    published = graph.publish()
    archived = published.archive()

    assert published.status == GraphStatus.PUBLISHED
    assert archived.status == GraphStatus.ARCHIVED


def test_graph_rejects_invalid_entry_step() -> None:
    graph = build_graph().model_copy(update={"entry_step": "missing"})

    try:
        graph.model_validate(graph.model_dump())
    except ValueError as exc:
        assert "entry step references unknown node" in str(exc)
