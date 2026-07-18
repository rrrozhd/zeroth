"""Composition: the contract facade, and the public validator built on it.

``ContractValidator`` runs every contract-owned validator in the canonical
order. It is synchronous and needs nothing outside the contracts layer, so a
library consumer can validate graph structure on its own.

``GraphValidator`` is the public entry point. It adds the execution-level
checks -- parallel config and capability grants -- that need the runtime and
governance layers, which is why it is composed outside ``contracts``.
"""

from __future__ import annotations

import pytest

from zeroth.contracts.graph.validation import ContractValidator
from zeroth.core.graph.models import (
    AgentNode,
    AgentNodeData,
    Edge,
    ExecutionSettings,
    Graph,
)
from zeroth.core.graph.validation_errors import ValidationCode, ValidationIssue
from zeroth.core.parallel.models import ParallelConfig


def _agent(node_id: str, **overrides: object) -> AgentNode:
    fields: dict[str, object] = {
        "node_id": node_id,
        "graph_version_ref": "g@1",
        "input_contract_ref": "contract://in",
        "output_contract_ref": "contract://out",
        "agent": AgentNodeData(instruction="go", model_provider="p"),
    }
    fields.update(overrides)
    return AgentNode(**fields)  # type: ignore[arg-type]


def test_empty_graph_is_reported_first() -> None:
    graph = Graph(graph_id="g", name="G", policy_bindings=["  "])
    issues: list[ValidationIssue] = []
    ContractValidator().validate(graph, issues)

    assert [issue.code for issue in issues] == [
        ValidationCode.EMPTY_GRAPH,
        ValidationCode.INVALID_POLICY_REF,
        ValidationCode.MISSING_ENTRYPOINT,
    ]


def test_contract_validation_covers_the_whole_pipeline_in_order() -> None:
    """Graph refs, nodes, entrypoint, edges, tool attachments, then cycles."""
    graph = Graph(
        graph_id="g",
        name="G",
        entry_step="a",
        policy_bindings=["  "],
        execution_settings=ExecutionSettings(max_visits_per_edge=None),
        nodes=[_agent("a"), _agent("b", graph_version_ref="  ")],
        edges=[
            Edge(edge_id="e1", source_node_id="a", target_node_id="b"),
            Edge(edge_id="e2", source_node_id="b", target_node_id="a"),
        ],
    )
    issues: list[ValidationIssue] = []
    ContractValidator().validate(graph, issues)

    assert [issue.code for issue in issues] == [
        ValidationCode.INVALID_POLICY_REF,
        ValidationCode.INVALID_GRAPH_VERSION_REF,
        ValidationCode.UNSAFE_CYCLE,
    ]


def test_contract_validation_skips_governance_owned_rules() -> None:
    """No capability checks without an injected implementation."""
    graph = Graph(
        graph_id="g",
        name="G",
        entry_step="a",
        nodes=[_agent("a")],
    )
    graph.nodes[0].agent.mcp_servers = [{"name": "fs"}]
    issues: list[ValidationIssue] = []
    ContractValidator().validate(graph, issues)

    assert issues == []


def test_public_validator_resolves_to_one_class_from_both_paths() -> None:
    from zeroth.core.graph import validation as legacy
    from zeroth.runtime import graph_validation as canonical

    assert legacy.GraphValidator is canonical.GraphValidator


@pytest.mark.asyncio
async def test_public_validator_adds_the_execution_checks() -> None:
    from zeroth.runtime.graph_validation import GraphValidator

    graph = Graph(
        graph_id="g",
        name="G",
        entry_step="a",
        nodes=[
            _agent(
                "a",
                parallel_config=ParallelConfig(
                    split_path="payload.items",
                    merge_strategy="custom",
                    reducer_ref="zeroth.nope:fn",
                ),
            )
        ],
    )
    report = await GraphValidator().validate(graph)

    assert [issue.code for issue in report.issues] == [ValidationCode.INVALID_REDUCER_REF]
