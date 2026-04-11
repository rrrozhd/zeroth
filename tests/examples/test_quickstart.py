"""Tests for zeroth.core.examples.quickstart tutorial helpers.

These tests lock in the shape of ``build_demo_graph`` so the Getting Started
tutorial and Governance Walkthrough can rely on it remaining a trivial
~10-line import.
"""

from __future__ import annotations

from zeroth.core.examples.quickstart import (
    build_demo_graph,
    build_demo_graph_with_policy,
)
from zeroth.core.graph.models import (
    AgentNode,
    Edge,
    ExecutableUnitNode,
    Graph,
    HumanApprovalNode,
)
from zeroth.core.policy.models import Capability


def test_build_demo_graph_returns_graph_instance() -> None:
    graph = build_demo_graph()
    assert isinstance(graph, Graph)
    assert graph.graph_id
    assert graph.name


def test_build_demo_graph_has_agent_and_tool_nodes() -> None:
    graph = build_demo_graph()
    agent_nodes = [n for n in graph.nodes if isinstance(n, AgentNode)]
    tool_nodes = [n for n in graph.nodes if isinstance(n, ExecutableUnitNode)]
    assert len(agent_nodes) == 1
    assert len(tool_nodes) == 1
    assert agent_nodes[0].node_id == "agent"
    assert tool_nodes[0].node_id == "tool"


def test_build_demo_graph_edges_connect_all_nodes() -> None:
    graph = build_demo_graph()
    assert len(graph.edges) >= 1
    node_ids = {n.node_id for n in graph.nodes}
    for edge in graph.edges:
        assert isinstance(edge, Edge)
        assert edge.source_node_id in node_ids
        assert edge.target_node_id in node_ids


def test_build_demo_graph_with_approval_inserts_human_node() -> None:
    graph = build_demo_graph(include_approval=True)
    approval_nodes = [n for n in graph.nodes if isinstance(n, HumanApprovalNode)]
    assert len(approval_nodes) == 1
    assert approval_nodes[0].node_id == "approval"
    # Chain: agent -> approval -> tool
    source_to_target = {(e.source_node_id, e.target_node_id) for e in graph.edges}
    assert ("agent", "approval") in source_to_target
    assert ("approval", "tool") in source_to_target


def test_build_demo_graph_uses_supplied_instruction_and_model() -> None:
    graph = build_demo_graph(
        instruction="be helpful",
        llm_model="openai/gpt-4o-mini",
    )
    agent = next(n for n in graph.nodes if isinstance(n, AgentNode))
    assert agent.agent.instruction == "be helpful"
    assert agent.agent.model_provider == "openai/gpt-4o-mini"


def test_build_demo_graph_with_policy_sets_tool_policy_bindings() -> None:
    graph = build_demo_graph_with_policy(
        denied_capabilities=[Capability.NETWORK_WRITE, Capability.FILESYSTEM_WRITE]
    )
    tool = next(n for n in graph.nodes if isinstance(n, ExecutableUnitNode))
    assert "block-demo-caps" in tool.policy_bindings


def test_quickstart_module_declares_unstable_api() -> None:
    import zeroth.core.examples.quickstart as qs

    docstring = (qs.__doc__ or "").lower()
    assert "tutorial" in docstring
    assert "not a stable" in docstring or "unstable" in docstring
