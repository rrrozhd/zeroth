from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import UTC, datetime

import pytest

import zeroth.contracts.graph.models as graph_models
import zeroth.contracts.graph.versioning as graph_versioning
from zeroth.contracts.governed.app.spec import GovernedFlowSpec

from zeroth.contracts.graph.models import (
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
    IfNode,
    IfNodeData,
    LoopNode,
    LoopNodeData,
)
from zeroth.contracts.graph.warnings import LegacyEngineDeprecationWarning
from zeroth.contracts.graph.serialization import deserialize_graph, serialize_graph
from zeroth.contracts.mappings.models import (
    ConstantMappingOperation,
    DefaultMappingOperation,
    EdgeMapping,
    PassthroughMappingOperation,
    RenameMappingOperation,
)
from zeroth.platform.primitives import utc_now


def test_graph_models_consume_platform_clock_per_instance(monkeypatch) -> None:
    assert graph_models.Graph.model_fields["created_at"].default_factory is utc_now
    assert graph_models.Graph.model_fields["updated_at"].default_factory is utc_now

    first = build_graph()
    second = build_graph()
    assert first.created_at.tzinfo is UTC
    assert first.created_at is not second.created_at

    fixed = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(graph_models, "utc_now", lambda: fixed)

    transitioned = first.transition_to(first.status)

    assert transitioned.updated_at == fixed


def test_graph_versioning_consumes_platform_clock(monkeypatch) -> None:
    fixed = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(graph_versioning, "utc_now", lambda: fixed)
    graph = build_graph()

    cloned = graph_versioning.clone_graph_version(
        graph,
        version=graph.version + 1,
        status=graph_models.GraphStatus.DRAFT,
    )

    assert cloned.created_at == fixed
    assert cloned.updated_at == fixed


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


@pytest.mark.parametrize("authored_value", [False, True])
def test_graph_serialization_preserves_authored_engine_flag(authored_value: bool) -> None:
    with pytest.warns(LegacyEngineDeprecationWarning) if authored_value is False else nullcontext():
        settings = ExecutionSettings(sequential_join_enabled=authored_value)
    graph = build_graph().model_copy(update={"execution_settings": settings})

    encoded = serialize_graph(graph)
    with pytest.warns(LegacyEngineDeprecationWarning) if authored_value is False else nullcontext():
        decoded = deserialize_graph(encoded)

    assert json.loads(encoded)["execution_settings"]["sequential_join_enabled"] is authored_value
    assert decoded.execution_settings.sequential_join_enabled is authored_value
    assert "sequential_join_enabled" in decoded.execution_settings.model_fields_set


def test_graph_serialization_preserves_absent_engine_flag() -> None:
    graph = build_graph().model_copy(update={"execution_settings": ExecutionSettings()})

    encoded = serialize_graph(graph)
    decoded = deserialize_graph(encoded)

    assert "sequential_join_enabled" not in json.loads(encoded)["execution_settings"]
    assert decoded.execution_settings.sequential_join_enabled is False
    assert "sequential_join_enabled" not in decoded.execution_settings.model_fields_set


def test_unauthored_engine_flag_selects_structured_token_mode() -> None:
    from zeroth.contracts.graph.engine_mode import token_engine_enabled

    settings = ExecutionSettings()

    # ABI remains pinned while authored-field presence controls effective mode.
    assert settings.sequential_join_enabled is False
    assert "sequential_join_enabled" not in settings.model_fields_set
    assert token_engine_enabled(settings) is True


@pytest.mark.parametrize(("authored", "expected"), [(True, True), (False, False)])
def test_authored_engine_flag_controls_effective_mode(authored: bool, expected: bool) -> None:
    from zeroth.contracts.graph.engine_mode import token_engine_enabled

    with pytest.warns(LegacyEngineDeprecationWarning) if authored is False else nullcontext():
        settings = ExecutionSettings(sequential_join_enabled=authored)

    assert token_engine_enabled(settings) is expected


def test_explicit_legacy_engine_emits_structured_validation_warning() -> None:
    with pytest.warns(LegacyEngineDeprecationWarning) as captured:
        ExecutionSettings(sequential_join_enabled=False)

    warning = captured[0].message
    assert warning.code == "legacy_engine_deprecated"
    assert warning.stage == "graph_validation"
    assert warning.engine_mode == "legacy"


def test_graph_compiles_to_governai_flow_spec() -> None:
    spec = build_graph().to_governed_flow_spec()

    assert isinstance(spec, GovernedFlowSpec)
    assert spec.name == "Governed Demo"
    assert spec.entry_step == "agent-step"
    assert spec.policies == [{"ref": "policy://safety"}]
    assert spec.steps[0].agent["kind"] == "agent_ref"
    assert spec.steps[0].transition.kind == "branch"
    assert spec.steps[0].transition.mapping == {
        "payload.user.id is not None": "tool-step"
    }
    assert spec.steps[1].tool["kind"] == "executable_unit_ref"
    assert spec.steps[1].transition.kind == "then"
    assert spec.steps[2].agent["kind"] == "human_approval_ref"
    assert spec.steps[2].transition.kind == "end"


def test_single_conditional_edge_compiles_as_a_branch() -> None:
    decision = IfNode(
        node_id="quality-gate",
        graph_version_ref="graph-1@1",
        condition=IfNodeData(expression="payload.ready == True"),
    )
    graph = Graph(
        graph_id="graph-1",
        name="One connected outcome",
        entry_step="quality-gate",
        nodes=[decision, build_graph().nodes[1]],
        edges=[
            Edge(
                edge_id="true-route",
                source_node_id="quality-gate",
                target_node_id="tool-step",
                condition=Condition(expression="payload.zeroth_if['quality-gate'].route == 'true'"),
            )
        ],
    )

    transition = graph.to_governed_flow_spec().steps[0].transition

    assert transition.kind == "branch"
    assert transition.mapping == {
        "payload.zeroth_if['quality-gate'].route == 'true'": "tool-step"
    }


@pytest.mark.parametrize(
    "routes",
    [
        [{"route_id": "only", "label": "Only", "is_default": True}],
        [
            {"route_id": "same", "label": "First", "match_value": "a"},
            {"route_id": "same", "label": "Fallback", "is_default": True},
        ],
        [
            {"route_id": "a", "label": "A", "match_value": "a"},
            {"route_id": "b", "label": "B", "match_value": "b"},
        ],
        [
            {"route_id": "a", "label": "A", "is_default": True},
            {"route_id": "b", "label": "B", "is_default": True},
        ],
        [
            {"route_id": "a", "label": "A", "match_value": "same"},
            {"route_id": "b", "label": "B", "match_value": "same"},
            {"route_id": "fallback", "label": "Fallback", "is_default": True},
        ],
        [
            {"route_id": "a", "label": "A", "match_value": {"nested": True}},
            {"route_id": "fallback", "label": "Fallback", "is_default": True},
        ],
        [
            {"route_id": "a", "label": "A", "match_value": "a"},
            {
                "route_id": "fallback",
                "label": "Fallback",
                "match_value": ["unused", "but", "invalid"],
                "is_default": True,
            },
        ],
        [
            *[
                {"route_id": f"case-{index}", "label": f"Case {index}", "match_value": index}
                for index in range(12)
            ],
            {"route_id": "fallback", "label": "Fallback", "is_default": True},
        ],
    ],
)
def test_if_node_rejects_ambiguous_or_non_scalar_route_configuration(
    routes: list[dict[str, object]],
) -> None:
    with pytest.raises(ValueError):
        IfNodeData(expression="payload.kind", routes=routes)


def test_if_node_route_matches_are_type_sensitive() -> None:
    condition = IfNodeData(
        expression="payload.value",
        routes=[
            {"route_id": "boolean", "label": "Boolean", "match_value": True},
            {"route_id": "integer", "label": "Integer", "match_value": 1},
            {"route_id": "fallback", "label": "Fallback", "is_default": True},
        ],
    )

    assert [route.route_id for route in condition.routes] == [
        "boolean",
        "integer",
        "fallback",
    ]


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


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        (ExecutionSettings(max_visits_per_node=4, max_visits_per_edge=4), "max_visits_per_node"),
        (ExecutionSettings(max_visits_per_node=5, max_visits_per_edge=3), "max_visits_per_edge"),
    ],
)
def test_graph_rejects_safety_limits_that_preempt_loop_outcomes(
    settings: ExecutionSettings,
    expected: str,
) -> None:
    loop = LoopNode(
        node_id="retry",
        graph_version_ref="loop@1",
        loop=LoopNodeData(until="payload.ready == True", max_retries=3),
    )

    with pytest.raises(ValueError, match=expected):
        Graph(
            graph_id="loop",
            name="Bounded loop",
            entry_step="retry",
            nodes=[loop],
            execution_settings=settings,
        )


def test_graph_accepts_safety_limits_that_allow_loop_to_emit_limit() -> None:
    graph = Graph(
        graph_id="loop",
        name="Bounded loop",
        entry_step="retry",
        nodes=[
            LoopNode(
                node_id="retry",
                graph_version_ref="loop@1",
                loop=LoopNodeData(until="payload.ready == True", max_retries=3),
            )
        ],
        execution_settings=ExecutionSettings(max_visits_per_node=5, max_visits_per_edge=4),
    )

    assert graph.execution_settings.max_visits_per_node == 5
