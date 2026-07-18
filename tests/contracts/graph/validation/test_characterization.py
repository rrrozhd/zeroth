"""Characterization of :class:`GraphValidator` output, locked before decomposition.

Graph validation is a *contract*: consumers branch on issue codes, Studio
renders ``path`` to highlight the offending field, and the console shows
``message`` verbatim. The order matters too -- the report is a flat list, and
the first error is the one an author sees first.

Splitting the validator into per-concern modules and re-composing them is
exactly the change that can silently reorder or reword that list while every
existing per-rule test keeps passing. These expectations were captured from the
pre-decomposition implementation and must not be edited to accommodate a
refactor: a diff here is a behavior regression, not a test that needs updating.

``multi_validator`` is the load-bearing case. It trips all seven validators at
once, so it pins the concatenation order across them, which single-issue cases
structurally cannot see.
"""

from __future__ import annotations

import pytest

from zeroth.core.graph.validation import GraphValidator

from ._graphs import BUILDERS


# (severity, code, message, path, node_id, edge_id) for every issue, in order.
EXPECTED: dict[str, list[tuple[str, str, str, tuple[str, ...], str | None, str | None]]] = {
    "empty_graph": [
        ("error", "empty_graph", "graph must contain at least one node", (), None, None),
        (
            "error",
            "missing_entrypoint",
            "graph entrypoint is required",
            ("entry_step",),
            None,
            None,
        ),
    ],
    "multi_validator": [
        (
            "error",
            "invalid_policy_ref",
            "invalid policy reference: '   '",
            ("policy_bindings",),
            None,
            None,
        ),
        (
            "error",
            "invalid_graph_version_ref",
            "invalid graph version ref on node 'agent'",
            ("nodes", "agent", "graph_version_ref"),
            "agent",
            None,
        ),
        (
            "error",
            "missing_contract_ref",
            "input contract ref is required",
            ("nodes", "agent", "input_contract_ref"),
            "agent",
            None,
        ),
        (
            "error",
            "invalid_output_contract",
            "output contract ref is required",
            ("nodes", "agent", "output_contract_ref"),
            "agent",
            None,
        ),
        (
            "error",
            "invalid_policy_ref",
            "invalid node policy reference",
            ("nodes", "agent", "policy_bindings", "1"),
            "agent",
            None,
        ),
        (
            "error",
            "invalid_capability_ref",
            "invalid capability reference",
            ("nodes", "agent", "capability_bindings", "1"),
            "agent",
            None,
        ),
        (
            "error",
            "invalid_node_attachment",
            "approval payload schema ref is required",
            ("nodes", "approval", "human_approval", "approval_payload_schema_ref"),
            "approval",
            None,
        ),
        (
            "error",
            "invalid_node_attachment",
            "resolution schema ref is required",
            ("nodes", "approval", "human_approval", "resolution_schema_ref"),
            "approval",
            None,
        ),
        ("error", "duplicate_node_id", "duplicate node id: agent", (), "agent", None),
        (
            "error",
            "unknown_entrypoint",
            "entry_step must point at the entrypoint node",
            ("entry_step",),
            "entry",
            None,
        ),
        (
            "error",
            "invalid_node_attachment",
            "the entrypoint node cannot have incoming edges",
            ("edges", "e2"),
            "entry",
            "e2",
        ),
        ("error", "duplicate_edge_id", "duplicate edge id: e1", (), None, "e1"),
        (
            "error",
            "invalid_tool_edge",
            "tool edge source must be an agent node",
            ("edges", "e3", "source_node_id"),
            None,
            "e3",
        ),
        (
            "error",
            "invalid_tool_edge",
            "tool edge target must be an executable unit or code node",
            ("edges", "e3", "target_node_id"),
            None,
            "e3",
        ),
        (
            "error",
            "invalid_tool_edge",
            "tool edges cannot carry conditions or mappings",
            ("edges", "e3"),
            None,
            "e3",
        ),
        (
            "error",
            "invalid_condition",
            "condition expression is required",
            ("edges", "e3", "condition", "expression"),
            None,
            "e3",
        ),
        (
            "error",
            "invalid_tool_binding",
            "tool binding 'ghost' points at 'ghost-unit', which is not attached by a tool edge",
            ("nodes", "agent", "agent", "tool_bindings"),
            "agent",
            None,
        ),
        ("error", "unsafe_cycle", "cyclic graph path must declare a safeguard", (), None, None),
        (
            "error",
            "invalid_reducer_ref",
            "invalid reducer_ref on node 'agent': reducer_ref 'not a dotted path' is not a valid dotted import path; expected pattern: module.submodule.function",
            ("nodes", "agent", "parallel_config", "reducer_ref"),
            "agent",
            None,
        ),
    ],
    "tool_attachment": [
        (
            "error",
            "invalid_tool_binding",
            "attached tool 'unit-a' has multiple bindings",
            ("nodes", "agent", "agent", "tool_bindings"),
            "agent",
            None,
        ),
        (
            "error",
            "invalid_tool_binding",
            "attached tool 'unit-b' needs a binding with a name, description, and argument descriptions",
            ("nodes", "agent", "agent", "tool_bindings"),
            "agent",
            None,
        ),
        (
            "error",
            "invalid_tool_binding",
            "tool names must be unique per agent: dup",
            ("nodes", "agent", "agent", "tool_bindings"),
            "agent",
            None,
        ),
    ],
    "mcp_capability": [
        (
            "error",
            "missing_mcp_capability",
            "agent 'agent' declares mcp_servers but is missing external_api_call, process_spawn; add the missing capabilities to the agent's capability_bindings",
            ("nodes", "agent", "agent", "mcp_servers"),
            "agent",
            None,
        ),
    ],
    "inline_source": [
        (
            "error",
            "invalid_inline_source",
            "syntax error on line 1: invalid syntax",
            ("nodes", "code", "executable_unit", "inline_source"),
            "code",
            None,
        ),
    ],
    "empty_inline_source": [
        (
            "error",
            "invalid_inline_source",
            "code is required",
            ("nodes", "code", "executable_unit", "inline_source"),
            "code",
            None,
        ),
    ],
    "parallel_config": [
        (
            "error",
            "invalid_reducer_ref",
            "invalid reducer_ref on node 'agent': reducer_ref 'zeroth.does.not.exist:reducer' is not a valid dotted import path; expected pattern: module.submodule.function",
            ("nodes", "agent", "parallel_config", "reducer_ref"),
            "agent",
            None,
        ),
        (
            "warning",
            "invalid_merge_strategy",
            "merge_strategy='merge' on node 'merger' cannot be contract-checked because no ContractRegistry is wired; dict-shape will be enforced at runtime instead",
            ("nodes", "merger", "parallel_config", "merge_strategy"),
            "merger",
            None,
        ),
    ],
    "unsafe_cycle": [
        ("error", "unsafe_cycle", "cyclic graph path must declare a safeguard", (), None, None),
    ],
    "mapping_and_condition": [
        (
            "error",
            "invalid_condition",
            "condition expression is required",
            ("edges", "e1", "condition", "expression"),
            None,
            "e1",
        ),
        (
            "error",
            "invalid_condition",
            "invalid condition operand reference",
            ("edges", "e1", "condition", "operand_refs", "1"),
            None,
            "e1",
        ),
    ],
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(EXPECTED))
async def test_validator_output_is_unchanged(case: str) -> None:
    report = await GraphValidator().validate(BUILDERS[case]())
    actual = [
        (
            issue.severity.value,
            issue.code.value,
            issue.message,
            issue.path,
            issue.node_id,
            issue.edge_id,
        )
        for issue in report.issues
    ]
    assert actual == EXPECTED[case]


def test_every_builder_is_characterized() -> None:
    """A new representative graph must come with pinned expectations."""
    assert set(BUILDERS) == set(EXPECTED)
