"""Representative invalid graphs for the validation characterization suite.

Each builder returns a graph that trips a specific set of validators. The
``multi_validator`` graph deliberately trips several at once: it is the case
that pins the *concatenation order* across validators, which is the property
the decomposition can silently break.
"""

from __future__ import annotations

from collections.abc import Callable

from zeroth.contracts.graph.models import (
    AgentNode,
    AgentNodeData,
    AgentToolBinding,
    Condition,
    Edge,
    EntrypointNode,
    ExecutableUnitNode,
    ExecutableUnitNodeData,
    ExecutionSettings,
    Graph,
    GraphStatus,
    HumanApprovalNode,
    HumanApprovalNodeData,
)
from zeroth.runtime.parallel.models import ParallelConfig


def _agent(
    node_id: str,
    *,
    graph_version_ref: str = "g@1",
    input_contract_ref: str | None = "contract://in",
    output_contract_ref: str | None = "contract://out",
    policy_bindings: list[str] | None = None,
    capability_bindings: list[str] | None = None,
    instruction: str = "do the thing",
    model_provider: str = "governai:router",
    tool_bindings: list[AgentToolBinding] | None = None,
    mcp_servers: list[dict[str, object]] | None = None,
    parallel_config: ParallelConfig | None = None,
) -> AgentNode:
    return AgentNode(
        node_id=node_id,
        graph_version_ref=graph_version_ref,
        input_contract_ref=input_contract_ref,
        output_contract_ref=output_contract_ref,
        policy_bindings=policy_bindings or [],
        capability_bindings=capability_bindings or [],
        parallel_config=parallel_config,
        agent=AgentNodeData(
            instruction=instruction,
            model_provider=model_provider,
            tool_bindings=tool_bindings or [],
            mcp_servers=mcp_servers or [],
        ),
    )


def _unit(
    node_id: str,
    *,
    manifest_ref: str = "eu://unit",
    capability_bindings: list[str] | None = None,
) -> ExecutableUnitNode:
    return ExecutableUnitNode(
        node_id=node_id,
        graph_version_ref="g@1",
        input_contract_ref="contract://in",
        output_contract_ref="contract://out",
        capability_bindings=capability_bindings or [],
        executable_unit=ExecutableUnitNodeData(
            manifest_ref=manifest_ref,
            execution_mode="wrapped_command",
        ),
    )


def _inline_unit(node_id: str, source: str) -> ExecutableUnitNode:
    return ExecutableUnitNode(
        node_id=node_id,
        graph_version_ref="g@1",
        input_contract_ref="contract://in",
        output_contract_ref="contract://out",
        executable_unit=ExecutableUnitNodeData(
            execution_mode="inline",
            inline_source=source,
        ),
    )


def _graph(**overrides: object) -> Graph:
    base: dict[str, object] = {
        "graph_id": "g",
        "name": "Characterization Graph",
        "version": 1,
        "status": GraphStatus.DRAFT,
        "entry_step": "agent",
        "nodes": [],
        "edges": [],
        "execution_settings": ExecutionSettings(max_visits_per_edge=2),
    }
    base.update(overrides)
    return Graph(**base)  # type: ignore[arg-type]


def empty_graph() -> Graph:
    """No nodes at all, and an entry_step that cannot resolve."""
    return _graph(nodes=[], edges=[], entry_step=None)


def multi_validator() -> Graph:
    """Trips every validator in the pipeline at once.

    This is the ordering guard: graph refs, nodes, entrypoint, edges, tool
    attachments, cycles, and parallel config each contribute at least one
    issue, so a re-composition that reorders the validators fails here.

    ``Graph`` itself rejects an unknown ``entry_step`` and edges naming unknown
    nodes, so the entrypoint rule exercised here is the dedicated
    ``EntrypointNode`` one rather than a dangling ``entry_step``.
    """
    agent = _agent(
        "agent",
        graph_version_ref="   ",
        input_contract_ref=None,
        output_contract_ref="  ",
        policy_bindings=["ok://policy", "  "],
        capability_bindings=["capability://filesystem-read", " "],
        tool_bindings=[
            AgentToolBinding(
                target_node_id="ghost-unit",
                name="ghost",
                description="points at nothing attached",
            )
        ],
        parallel_config=ParallelConfig(
            split_path="payload.items",
            merge_strategy="custom",
            reducer_ref="not a dotted path",
        ),
    )
    unit = _unit("unit", capability_bindings=["network_access"])
    approval = HumanApprovalNode(
        node_id="approval",
        graph_version_ref="g@1",
        input_contract_ref="contract://in",
        output_contract_ref="contract://out",
        human_approval=HumanApprovalNodeData(
            approval_payload_schema_ref="  ",
            resolution_schema_ref=None,
        ),
    )
    entrypoint = EntrypointNode(
        node_id="entry",
        graph_version_ref="g@1",
        input_contract_ref="contract://in",
        output_contract_ref="contract://out",
    )
    return _graph(
        # Points at the agent while a dedicated entrypoint node exists.
        entry_step="agent",
        policy_bindings=["   "],
        execution_settings=ExecutionSettings(max_visits_per_edge=None),
        nodes=[agent, unit, approval, entrypoint, _agent("agent")],
        edges=[
            # Cycle with no safeguard: agent -> unit -> agent.
            Edge(edge_id="e1", source_node_id="agent", target_node_id="unit"),
            Edge(edge_id="e1", source_node_id="unit", target_node_id="agent"),
            # Nothing may flow into the entrypoint node.
            Edge(edge_id="e2", source_node_id="approval", target_node_id="entry"),
            # Tool edge with inverted endpoints and a forbidden condition.
            Edge(
                edge_id="e3",
                source_node_id="unit",
                target_node_id="agent",
                kind="tool",
                condition=Condition(expression="  "),
            ),
        ],
    )


def tool_attachment_graph() -> Graph:
    """Attached tools without bindings, duplicate names, and short capability grants."""
    agent = _agent(
        "agent",
        capability_bindings=[],
        tool_bindings=[
            AgentToolBinding(target_node_id="unit-a", name="dup", description="one"),
            AgentToolBinding(target_node_id="unit-a", name="dup", description="two"),
        ],
    )
    return _graph(
        nodes=[agent, _unit("unit-a", capability_bindings=["network_access"]), _unit("unit-b")],
        edges=[
            Edge(edge_id="t1", source_node_id="agent", target_node_id="unit-a", kind="tool"),
            Edge(edge_id="t2", source_node_id="agent", target_node_id="unit-b", kind="tool"),
        ],
    )


def mcp_capability_graph() -> Graph:
    """An agent declaring MCP servers without the required capability grants."""
    return _graph(
        nodes=[_agent("agent", mcp_servers=[{"name": "fs"}])],
        edges=[],
    )


def inline_source_graph() -> Graph:
    """Inline code that does not compile."""
    return _graph(
        entry_step="code",
        nodes=[_inline_unit("code", "def broken(:\n    pass\n")],
        edges=[],
    )


def empty_inline_source_graph() -> Graph:
    """Inline code node with blank source."""
    return _graph(
        entry_step="code",
        nodes=[_inline_unit("code", "   \n  ")],
        edges=[],
    )


def parallel_config_graph() -> Graph:
    """A custom reducer that cannot resolve, plus an unverifiable merge strategy."""
    return _graph(
        nodes=[
            _agent(
                "agent",
                parallel_config=ParallelConfig(
                    split_path="payload.items",
                    merge_strategy="custom",
                    reducer_ref="zeroth.does.not.exist:reducer",
                ),
            ),
            _agent(
                "merger",
                parallel_config=ParallelConfig(
                    split_path="payload.items",
                    merge_strategy="merge",
                ),
            ),
        ],
        edges=[Edge(edge_id="e1", source_node_id="agent", target_node_id="merger")],
    )


def unsafe_cycle_graph() -> Graph:
    """A two-node cycle with no safeguard configured."""
    return _graph(
        execution_settings=ExecutionSettings(max_visits_per_edge=None),
        nodes=[_agent("agent"), _agent("second")],
        edges=[
            Edge(edge_id="e1", source_node_id="agent", target_node_id="second"),
            Edge(edge_id="e2", source_node_id="second", target_node_id="agent"),
        ],
    )


def mapping_and_condition_graph() -> Graph:
    """An edge carrying an empty condition expression and an invalid mapping."""
    return _graph(
        nodes=[_agent("agent"), _agent("second")],
        edges=[
            Edge(
                edge_id="e1",
                source_node_id="agent",
                target_node_id="second",
                condition=Condition(expression="   ", operand_refs=["ok", "  "]),
            )
        ],
    )


BUILDERS: dict[str, Callable[[], Graph]] = {
    "empty_graph": empty_graph,
    "multi_validator": multi_validator,
    "tool_attachment": tool_attachment_graph,
    "mcp_capability": mcp_capability_graph,
    "inline_source": inline_source_graph,
    "empty_inline_source": empty_inline_source_graph,
    "parallel_config": parallel_config_graph,
    "unsafe_cycle": unsafe_cycle_graph,
    "mapping_and_condition": mapping_and_condition_graph,
}
