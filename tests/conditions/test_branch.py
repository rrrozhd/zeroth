from __future__ import annotations

from zeroth.conditions import (
    BranchResolver,
    ConditionBinder,
    ConditionContext,
    NextStepPlanner,
    TraversalState,
)
from zeroth.graph.models import (
    AgentNode,
    AgentNodeData,
    Edge,
    ExecutionSettings,
    Graph,
)
from zeroth.graph.models import (
    Condition as GraphCondition,
)


def build_graph(edges: list[Edge]) -> Graph:
    version_ref = "graph-1@1"
    return Graph(
        graph_id="graph-1",
        name="Branch Graph",
        entry_step="node-a",
        execution_settings=ExecutionSettings(
            max_total_steps=20,
            max_visits_per_node=3,
            max_visits_per_edge=3,
        ),
        nodes=[
            AgentNode(
                node_id="node-a",
                graph_version_ref=version_ref,
                input_contract_ref="contract://input",
                output_contract_ref="contract://output",
                agent=AgentNodeData(
                    instruction="start",
                    model_provider="governai:model",
                ),
            ),
            AgentNode(
                node_id="node-b",
                graph_version_ref=version_ref,
                input_contract_ref="contract://input",
                output_contract_ref="contract://output",
                agent=AgentNodeData(
                    instruction="middle",
                    model_provider="governai:model",
                ),
            ),
            AgentNode(
                node_id="node-c",
                graph_version_ref=version_ref,
                input_contract_ref="contract://input",
                output_contract_ref="contract://output",
                agent=AgentNodeData(
                    instruction="end",
                    model_provider="governai:model",
                ),
            ),
        ],
        edges=edges,
    )


def test_condition_binder_projects_graph_edges() -> None:
    graph = build_graph(
        [
            Edge(edge_id="edge-ab", source_node_id="node-a", target_node_id="node-b"),
            Edge(
                edge_id="edge-ac",
                source_node_id="node-a",
                target_node_id="node-c",
                condition=GraphCondition(expression="payload.route == 'c'"),
            ),
        ]
    )

    bindings = ConditionBinder().bind_graph(graph)

    assert [binding.edge_id for binding in bindings] == ["edge-ab", "edge-ac"]
    assert bindings[1].condition is not None
    assert bindings[1].target_node_id == "node-c"


def test_branch_resolver_supports_one_to_one_and_fan_out() -> None:
    graph = build_graph(
        [
            Edge(edge_id="edge-ab", source_node_id="node-a", target_node_id="node-b"),
            Edge(edge_id="edge-ac", source_node_id="node-a", target_node_id="node-c"),
        ]
    )

    resolution = BranchResolver().resolve(graph, "node-a", ConditionContext(payload={}))

    assert resolution.active_edge_ids == ["edge-ab", "edge-ac"]
    assert resolution.next_node_ids == ["node-b", "node-c"]
    assert resolution.suppressed_edge_ids == []


def test_branch_resolver_suppresses_false_conditions() -> None:
    graph = build_graph(
        [
            Edge(
                edge_id="edge-ab",
                source_node_id="node-a",
                target_node_id="node-b",
                condition=GraphCondition(expression="payload.route == 'b'"),
            )
        ]
    )

    resolution = BranchResolver().resolve(graph, "node-a", ConditionContext(payload={"route": "c"}))

    assert resolution.active_edge_ids == []
    assert resolution.next_node_ids == []
    assert resolution.suppressed_edge_ids == ["edge-ab"]
    assert resolution.terminal_reason == "branch_suppressed"


def test_branch_resolver_allows_cycle_traversal_with_safeguard() -> None:
    graph = build_graph(
        [
            Edge(
                edge_id="edge-ab",
                source_node_id="node-a",
                target_node_id="node-b",
                condition=GraphCondition(expression="True", allow_cycle_traversal=True),
            ),
            Edge(
                edge_id="edge-ba",
                source_node_id="node-b",
                target_node_id="node-a",
                condition=GraphCondition(expression="True", allow_cycle_traversal=True),
            ),
        ]
    )
    traversal_state = TraversalState(
        node_visit_counts={"node-a": 1, "node-b": 1},
        edge_visit_counts={"edge-ab": 1, "edge-ba": 1},
        path=["node-a", "node-b"],
    )

    resolution = BranchResolver().resolve(
        graph,
        "node-b",
        ConditionContext(payload={}),
        traversal_state=traversal_state,
    )

    assert resolution.next_node_ids == ["node-a"]
    assert resolution.condition_results[0].matched is True


def test_next_step_planner_wraps_branch_resolution() -> None:
    graph = build_graph(
        [
            Edge(
                edge_id="edge-ab",
                source_node_id="node-a",
                target_node_id="node-b",
                condition=GraphCondition(expression="payload.route == 'b'"),
            )
        ]
    )

    plan = NextStepPlanner().plan(graph, "node-a", ConditionContext(payload={"route": "b"}))

    assert plan.current_node_id == "node-a"
    assert plan.next_node_ids == ["node-b"]
    assert plan.branch_resolution.active_edge_ids == ["edge-ab"]
    assert plan.branch_resolution.condition_results[0].matched is True
