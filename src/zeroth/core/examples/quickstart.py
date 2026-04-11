"""Quickstart graph builders for the Phase 30 docs site.

Tutorial helper. NOT a stable API. Subject to change without a deprecation
cycle. See Phase 30 docs for context.

These helpers give the Getting Started tutorial and the Governance
Walkthrough a single, tested function that returns a minimal but valid
:class:`~zeroth.core.graph.models.Graph`, so the example scripts on the
docs site can stay around ten lines instead of eighty.
"""

from __future__ import annotations

from zeroth.core.graph.models import (
    AgentNode,
    AgentNodeData,
    DisplayMetadata,
    Edge,
    ExecutableUnitNode,
    ExecutableUnitNodeData,
    Graph,
    HumanApprovalNode,
    HumanApprovalNodeData,
)
from zeroth.core.policy.models import Capability

_DEMO_GRAPH_ID = "demo-quickstart"
_DEMO_GRAPH_VERSION_REF = f"{_DEMO_GRAPH_ID}@1"
_DEFAULT_INSTRUCTION = (
    "You are a friendly assistant. Answer the user briefly, then call the "
    "`echo` tool with your answer."
)
_DEFAULT_MODEL = "openai/gpt-4o-mini"
_DEMO_POLICY_ID = "block-demo-caps"


def build_demo_graph(
    instruction: str = _DEFAULT_INSTRUCTION,
    llm_model: str = _DEFAULT_MODEL,
    *,
    include_approval: bool = False,
) -> Graph:
    """Return a minimal demo :class:`Graph` used by the Phase 30 tutorials.

    The graph contains one :class:`AgentNode` (``node_id="agent"``) wired to
    one :class:`ExecutableUnitNode` (``node_id="tool"``). When
    ``include_approval`` is true, a :class:`HumanApprovalNode`
    (``node_id="approval"``) is spliced between them so the Governance
    Walkthrough can demonstrate the approval gate without rebuilding the
    graph from scratch.

    Args:
        instruction: Instruction prompt for the agent node.
        llm_model: LiteLLM-style model identifier, e.g. ``openai/gpt-4o-mini``.
        include_approval: If ``True``, insert a human-approval node between
            the agent and the tool.

    Returns:
        A fully-validated :class:`Graph` ready to register with a
        :class:`~zeroth.core.graph.repository.GraphRepository`.

    Note:
        This helper is **not** a stable public API. It exists solely to keep
        the docs-site code snippets short.
    """
    agent = AgentNode(
        node_id="agent",
        graph_version_ref=_DEMO_GRAPH_VERSION_REF,
        display=DisplayMetadata(title="Demo agent"),
        input_contract_ref="contract://demo-input",
        output_contract_ref="contract://demo-output",
        agent=AgentNodeData(
            instruction=instruction,
            model_provider=llm_model,
        ),
    )
    tool = ExecutableUnitNode(
        node_id="tool",
        graph_version_ref=_DEMO_GRAPH_VERSION_REF,
        display=DisplayMetadata(title="Demo echo tool"),
        input_contract_ref="contract://demo-input",
        output_contract_ref="contract://demo-output",
        executable_unit=ExecutableUnitNodeData(
            manifest_ref="manifest://demo-echo",
            execution_mode="wrapped_command",
        ),
    )

    nodes: list[AgentNode | ExecutableUnitNode | HumanApprovalNode] = [agent]
    edges: list[Edge] = []

    if include_approval:
        approval = HumanApprovalNode(
            node_id="approval",
            graph_version_ref=_DEMO_GRAPH_VERSION_REF,
            display=DisplayMetadata(title="Human approval"),
            input_contract_ref="contract://demo-input",
            output_contract_ref="contract://demo-output",
            human_approval=HumanApprovalNodeData(
                approval_policy_config={"allow_edits": True},
            ),
        )
        nodes.append(approval)
        edges.append(
            Edge(edge_id="edge-agent-approval", source_node_id="agent", target_node_id="approval")
        )
        edges.append(
            Edge(edge_id="edge-approval-tool", source_node_id="approval", target_node_id="tool")
        )
    else:
        edges.append(Edge(edge_id="edge-agent-tool", source_node_id="agent", target_node_id="tool"))

    nodes.append(tool)

    return Graph(
        graph_id=_DEMO_GRAPH_ID,
        name="Zeroth Quickstart Demo",
        entry_step="agent",
        nodes=nodes,
        edges=edges,
    )


def build_demo_graph_with_policy(denied_capabilities: list[Capability]) -> Graph:
    """Return the demo graph with its tool node bound to a demo policy.

    The policy id ``"block-demo-caps"`` is a *reference* only. The example
    script is responsible for registering a
    :class:`~zeroth.core.policy.models.PolicyDefinition` with that id that
    denies ``denied_capabilities`` — this helper does **not** persist a
    policy. Plan 30-04 (Governance Walkthrough) uses this hook to
    demonstrate the policy-block scenario.

    Args:
        denied_capabilities: Capabilities the downstream tutorial script
            should encode into its registered policy. This function only
            records the intent via ``metadata`` so docs-site readers can
            see what the bound policy will forbid.

    Returns:
        A :class:`Graph` whose tool node has ``policy_bindings`` populated.
    """
    graph = build_demo_graph()
    tool = next(node for node in graph.nodes if node.node_id == "tool")
    new_tool = tool.model_copy(
        update={
            "policy_bindings": [_DEMO_POLICY_ID],
            "capability_bindings": [cap.value for cap in denied_capabilities],
        }
    )
    new_nodes = [new_tool if node.node_id == "tool" else node for node in graph.nodes]
    return graph.model_copy(update={"nodes": new_nodes})
