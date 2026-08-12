"""Node, node-type, and entrypoint validation owned by the contracts layer.

One rule inside agent-node validation -- the MCP capability grant check --
needs the governance ``Capability`` enum, which the contracts layer may not
import. It also fires in the *middle* of a node's issue sequence, so it cannot
simply be appended by a later pass without reordering the report. It is
therefore injected as a collaborator and invoked at its original position.
"""

from __future__ import annotations

from zeroth.contracts.graph.limits import (
    AGENT_INSTRUCTION_MAX_CHARS,
    DESCRIPTION_MAX_CHARS,
    INLINE_SOURCE_MAX_CHARS,
)
from zeroth.contracts.graph.models import (
    AgentNode,
    AgentNodeData,
    AgentToolBinding,
    EntrypointNode,
    ExecutableUnitNode,
    ExecutableUnitNodeData,
    Graph,
    Node,
    ToolArgument,
)
from zeroth.contracts.graph.validation.capabilities import NullCapabilityChecks
from zeroth.contracts.graph.validation.nodes import (
    validate_entrypoint,
    validate_nodes,
)
from zeroth.contracts.graph.validation_errors import ValidationCode, ValidationIssue


def _agent(node_id: str = "agent", **data: object) -> AgentNode:
    payload: dict[str, object] = {
        "instruction": "do the thing",
        "model_provider": "governai:router",
    }
    payload.update(data)
    return AgentNode(
        node_id=node_id,
        graph_version_ref="g@1",
        input_contract_ref="contract://in",
        output_contract_ref="contract://out",
        agent=AgentNodeData(**payload),  # type: ignore[arg-type]
    )


def _graph(nodes: list[Node], **overrides: object) -> Graph:
    base: dict[str, object] = {
        "graph_id": "g",
        "name": "G",
        "nodes": nodes,
        "entry_step": nodes[0].node_id if nodes else None,
    }
    base.update(overrides)
    return Graph(**base)  # type: ignore[arg-type]


def _run_nodes(graph: Graph) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    validate_nodes(graph, {}, issues, capability_checks=NullCapabilityChecks())
    return issues


def test_duplicate_node_ids_are_reported_once_and_skip_further_checks() -> None:
    graph = _graph([_agent(), _agent()])
    node_map: dict[str, Node] = {}
    issues: list[ValidationIssue] = []
    validate_nodes(graph, node_map, issues, capability_checks=NullCapabilityChecks())

    assert [issue.code for issue in issues] == [ValidationCode.DUPLICATE_NODE_ID]
    assert set(node_map) == {"agent"}


def test_node_map_is_populated_for_later_passes() -> None:
    graph = _graph([_agent("a"), _agent("b")])
    node_map: dict[str, Node] = {}
    validate_nodes(graph, node_map, [], capability_checks=NullCapabilityChecks())

    assert set(node_map) == {"a", "b"}


def test_agent_requires_instruction_and_model_provider() -> None:
    graph = _graph([_agent(instruction="  ", model_provider="  ")])
    assert [issue.message for issue in _run_nodes(graph)] == [
        "agent instruction is required",
        "agent model provider is required",
    ]


def test_persist_conversation_requires_a_messages_key() -> None:
    graph = _graph([_agent(persist_conversation=True)])
    (issue,) = _run_nodes(graph)
    assert issue.message == "persist_conversation requires input_messages_key to be set"


def test_capability_checks_run_in_their_original_position() -> None:
    """The injected check must fire after the ref lists, before the next node."""
    calls: list[str] = []

    class Recording(NullCapabilityChecks):
        def validate_agent_capabilities(
            self,
            graph_id: str,
            node: AgentNode,
            issues: list[ValidationIssue],
        ) -> None:
            calls.append(node.node_id)

    graph = _graph([_agent("first"), _agent("second")])
    validate_nodes(graph, {}, [], capability_checks=Recording())

    assert calls == ["first", "second"]


def test_inline_source_must_compile() -> None:
    node = ExecutableUnitNode(
        node_id="code",
        graph_version_ref="g@1",
        input_contract_ref="contract://in",
        output_contract_ref="contract://out",
        executable_unit=ExecutableUnitNodeData(
            execution_mode="inline",
            inline_source="def broken(:\n",
        ),
    )
    (issue,) = _run_nodes(_graph([node]))
    assert issue.code is ValidationCode.INVALID_INLINE_SOURCE
    assert issue.message.startswith("syntax error on line 1:")


def test_inline_source_is_capped() -> None:
    node = ExecutableUnitNode(
        node_id="code",
        graph_version_ref="g@1",
        input_contract_ref="contract://in",
        output_contract_ref="contract://out",
        executable_unit=ExecutableUnitNodeData(
            execution_mode="inline",
            inline_source="x = 1\n" + "# pad\n" * INLINE_SOURCE_MAX_CHARS,
        ),
    )
    (issue,) = _run_nodes(_graph([node]))
    assert issue.message == f"code exceeds the {INLINE_SOURCE_MAX_CHARS} character limit"


def test_inline_cap_is_the_one_the_execution_unit_enforces() -> None:
    """The publish gate and the runtime must not drift apart."""
    from zeroth.integrations.execution import inline

    assert inline.INLINE_SOURCE_MAX_CHARS is INLINE_SOURCE_MAX_CHARS


# ---------------------------------------------------------------------------
# A05-5: the other authored strings that had no declared bound
# ---------------------------------------------------------------------------


def test_agent_instruction_is_capped() -> None:
    node = _agent("agent", instruction="a" * (AGENT_INSTRUCTION_MAX_CHARS + 1))

    (issue,) = _run_nodes(_graph([node]))

    assert issue.code is ValidationCode.INVALID_NODE_ATTACHMENT
    assert issue.message == (
        f"agent instruction exceeds the {AGENT_INSTRUCTION_MAX_CHARS} character limit"
    )
    assert issue.path == ("nodes", "agent", "agent", "instruction")


def test_an_instruction_at_exactly_the_cap_is_accepted() -> None:
    """The bound is inclusive -- an off-by-one here rejects legitimate authoring."""
    node = _agent("agent", instruction="a" * AGENT_INSTRUCTION_MAX_CHARS)

    assert _run_nodes(_graph([node])) == []


def test_tool_binding_description_is_capped() -> None:
    node = _agent(
        "agent",
        tool_bindings=[
            AgentToolBinding(
                target_node_id="unit",
                name="do_thing",
                description="d" * (DESCRIPTION_MAX_CHARS + 1),
            )
        ],
    )

    issues = _run_nodes(_graph([node]))

    assert any(
        issue.code is ValidationCode.INVALID_TOOL_BINDING
        and "description exceeds" in issue.message
        for issue in issues
    ), issues


def test_tool_argument_description_is_capped() -> None:
    node = _agent(
        "agent",
        tool_bindings=[
            AgentToolBinding(
                target_node_id="unit",
                name="do_thing",
                description="fine",
                arguments=[
                    ToolArgument(name="q", description="d" * (DESCRIPTION_MAX_CHARS + 1))
                ],
            )
        ],
    )

    issues = _run_nodes(_graph([node]))

    assert any(
        issue.code is ValidationCode.INVALID_TOOL_BINDING
        and "tool argument 'q' description exceeds" in issue.message
        for issue in issues
    ), issues


def test_descriptions_within_the_cap_raise_no_issue() -> None:
    node = _agent(
        "agent",
        tool_bindings=[
            AgentToolBinding(
                target_node_id="unit",
                name="do_thing",
                description="d" * DESCRIPTION_MAX_CHARS,
                arguments=[ToolArgument(name="q", description="d" * DESCRIPTION_MAX_CHARS)],
            )
        ],
    )

    issues = _run_nodes(_graph([node]))

    assert not [i for i in issues if "exceeds" in i.message], issues


def test_an_oversized_graph_still_loads_even_though_it_cannot_publish() -> None:
    """The cap is a publish gate, not a field bound.

    A ``Field(max_length=...)`` would make a graph persisted before this bound
    existed unloadable, turning an unpublishable graph into an unreadable one.
    """
    node = _agent("agent", instruction="a" * (AGENT_INSTRUCTION_MAX_CHARS + 1))

    assert len(node.agent.instruction) == AGENT_INSTRUCTION_MAX_CHARS + 1
    assert _run_nodes(_graph([node]))


def test_entrypoint_node_must_be_the_entry_step() -> None:
    entry = EntrypointNode(
        node_id="entry",
        graph_version_ref="g@1",
        input_contract_ref="contract://in",
        output_contract_ref="contract://out",
    )
    graph = _graph([_agent("agent"), entry], entry_step="agent")
    issues: list[ValidationIssue] = []
    validate_entrypoint(graph, {"agent": graph.nodes[0], "entry": entry}, issues)

    assert [issue.message for issue in issues] == ["entry_step must point at the entrypoint node"]


def test_missing_entrypoint_is_reported() -> None:
    graph = _graph([_agent()], entry_step=None)
    issues: list[ValidationIssue] = []
    validate_entrypoint(graph, {"agent": graph.nodes[0]}, issues)

    assert [issue.code for issue in issues] == [ValidationCode.MISSING_ENTRYPOINT]
