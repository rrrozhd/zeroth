"""Branch execution on a condition — example for docs/how-to/cookbook/condition-branch.md.

Builds a tiny graph in code (agent -> two branches -> approve/reject
tools) and uses :class:`BranchResolver` + :class:`ConditionContext` to
evaluate which branches are active for a given payload. Pure in-process —
no database or bootstrap required.
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    required_env: list[str] = []
    missing = [k for k in required_env if not os.environ.get(k)]
    if missing:
        print(f"SKIP: missing env vars: {', '.join(missing)}", file=sys.stderr)
        return 0

    from zeroth.core.conditions import BranchResolver, ConditionContext
    from zeroth.core.graph.models import (
        AgentNode,
        AgentNodeData,
        Condition,
        DisplayMetadata,
        Edge,
        ExecutableUnitNode,
        ExecutableUnitNodeData,
        Graph,
    )

    graph_version_ref = "demo-conditions@1"

    def _agent(node_id: str) -> AgentNode:
        return AgentNode(
            node_id=node_id,
            graph_version_ref=graph_version_ref,
            display=DisplayMetadata(title=node_id),
            input_contract_ref="contract://demo",
            output_contract_ref="contract://demo",
            agent=AgentNodeData(instruction="noop", model_provider="openai/gpt-4o-mini"),
        )

    def _tool(node_id: str) -> ExecutableUnitNode:
        return ExecutableUnitNode(
            node_id=node_id,
            graph_version_ref=graph_version_ref,
            display=DisplayMetadata(title=node_id),
            input_contract_ref="contract://demo",
            output_contract_ref="contract://demo",
            executable_unit=ExecutableUnitNodeData(
                manifest_ref=f"manifest://{node_id}",
                execution_mode="wrapped_command",
            ),
        )

    graph = Graph(
        graph_id="demo-conditions",
        name="Demo conditional branches",
        entry_step="classifier",
        nodes=[_agent("classifier"), _tool("approve"), _tool("reject")],
        edges=[
            Edge(
                edge_id="edge-approve",
                source_node_id="classifier",
                target_node_id="approve",
                condition=Condition(expression="payload.score >= 0.5"),
            ),
            Edge(
                edge_id="edge-reject",
                source_node_id="classifier",
                target_node_id="reject",
                condition=Condition(expression="payload.score < 0.5"),
            ),
        ],
    )

    resolver = BranchResolver()

    # Case 1: score=0.9 — only the approve branch should activate.
    high = resolver.resolve(
        graph,
        "classifier",
        ConditionContext(payload={"score": 0.9}),
    )
    print(f"score=0.9 active_edges={high.active_edge_ids} next={high.next_node_ids}")
    assert high.next_node_ids == ["approve"], high.next_node_ids

    # Case 2: score=0.2 — only the reject branch should activate.
    low = resolver.resolve(
        graph,
        "classifier",
        ConditionContext(payload={"score": 0.2}),
    )
    print(f"score=0.2 active_edges={low.active_edge_ids} next={low.next_node_ids}")
    assert low.next_node_ids == ["reject"], low.next_node_ids

    print("condition-branch demo OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
