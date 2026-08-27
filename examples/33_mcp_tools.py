"""33 — MCP tools: the pinned ``mcp_tool`` node and its capability ceiling.

What this shows
---------------
An MCP (Model Context Protocol) server advertises its tools at
``list_tools()`` on the day of the run — the opposite of what a governed
graph needs, which is a contract that exists *before* publish. Zeroth
closes that gap by importing each tool as its own graph node: an
:class:`~zeroth.contracts.graph.models.MCPToolNode` carrying the tool's
pinned name, description, input schema and ``schema_hash``, attached to
an agent by a ``kind="tool"`` edge.

This file builds the exact graph shape ``zeroth-core mcp-import`` writes,
then runs the real publish-time
:class:`~zeroth.runtime.graph_validation.GraphValidator` over four
variants of it so you can watch the capability model accept and refuse:

* the **floor** — every ``mcp_tool`` node must declare ``process_spawn``
  and ``external_api_call``, because reaching a server spawns a
  subprocess that calls out;
* the **ceiling** — a node may not declare more than the *operator*
  granted its server. ``capability_bindings`` are author-declared, so the
  server's ``grants`` are the one side of that check an author cannot
  edit;
* the **agent floor** — the agent binding the tool must itself hold what
  the node declares, or the runner's tool gate denies the call.

The fourth variant is the one worth reading twice: an agent holding a
capability the server does *not* grant still publishes. The ceiling's
subject is the tool node, never the agent — comparing the agent instead
would force an operator to widen ``grants`` until they covered every
capability any referencing agent happens to hold, which converges the
ceiling on "everything".

Hermetic on purpose
-------------------
No server is spawned and no model is called. The operator's registry is
stubbed by ``_grants_for`` below, which is exactly the seam
``bootstrap_service`` fills with a real database lookup. To exercise a
live server instead, register one (``POST /v1/mcp/servers`` with an
``mcp:admin`` key), list its tools (``GET /v1/mcp/servers/<ref>/tools``)
and import them with ``zeroth-core mcp-import``. See
``docs/how-to/mcp.md`` for the full register → import → publish → run
walkthrough.

Run
---
    uv run python examples/33_mcp_tools.py
"""

from __future__ import annotations

# Allow python examples/NN_name.py to find the sibling examples/_common.py helper.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import asyncio
import sys

from zeroth.contracts.graph.models import (
    AgentNode,
    AgentNodeData,
    AgentToolBinding,
    Capability,
    Edge,
    ExecutionSettings,
    Graph,
    MCPToolNode,
)
from zeroth.runtime.agents.mcp import MCP_REQUIRED_CAPABILITIES
from zeroth.runtime.graph_validation import GraphValidator

#: The floor as an author writes it into ``capability_bindings``. Imported
#: from the module the session pool enforces it in rather than respelled
#: here — publish and dispatch disagreeing about one node is precisely what
#: four independent copies of this pair used to cause.
FLOOR = sorted(capability.value for capability in MCP_REQUIRED_CAPABILITIES)

#: Stands in for the operator-owned registry row. ``bootstrap_service``
#: supplies the real version: a lookup in the ``mcp_server_configs`` table,
#: writable only with an ``mcp:admin`` key. The author's graph carries the
#: ``ref`` and nothing else — never the command, args or env.
REGISTERED_GRANTS: dict[str, set[Capability]] = {
    "docs-search": set(MCP_REQUIRED_CAPABILITIES) | {Capability.FILESYSTEM_READ},
}


async def _grants_for(server_ref: str) -> set[Capability] | None:
    """Resolve a ``server_ref`` to its grants, or ``None`` if unregistered."""
    return REGISTERED_GRANTS.get(server_ref)


def build_imported_graph(*, node_caps: list[str], agent_caps: list[str]) -> Graph:
    """The shape ``zeroth-core mcp-import`` writes, with both grants parametrised.

    The importer appends one ``MCPToolNode`` per tool carrying the floor, a
    ``kind="tool"`` edge from the agent, and an ``AgentToolBinding`` so the
    model can call it by name — and it tops the agent's own
    ``capability_bindings`` up to the floor as well, because clause (iii) of
    the predicate below would otherwise leave a draft that cannot publish.
    Both are parametrised here so the variants can show what each rule catches;
    ``agent_caps=[]`` is a graph an author hand-wrote, not the CLI's output.

    Note the ``mcp_tool`` node carries no ``input_contract_ref``: the pinned
    ``input_schema`` *is* its contract, and claiming a registered one as well
    is rejected rather than ignored.
    """
    return Graph(
        graph_id="support-triage",
        name="support-triage",
        version=1,
        entry_step="researcher",
        execution_settings=ExecutionSettings(max_total_steps=5),
        nodes=[
            AgentNode(
                node_id="researcher",
                graph_version_ref="support-triage@1",
                input_contract_ref="contract://question",
                output_contract_ref="contract://answer",
                capability_bindings=agent_caps,
                agent=AgentNodeData(
                    instruction="Answer using the documentation search tool.",
                    model_provider="openai/gpt-4o-mini",
                    tool_bindings=[
                        AgentToolBinding(
                            target_node_id="mcp_docs_search_read_file",
                            name="read_file",
                            description="Read a file from the documentation share.",
                        )
                    ],
                ),
            ),
            MCPToolNode.model_validate(
                {
                    "node_id": "mcp_docs_search_read_file",
                    "graph_version_ref": "support-triage@1",
                    "node_type": "mcp_tool",
                    "capability_bindings": node_caps,
                    "mcp_tool": {
                        "server_ref": "docs-search",
                        "tool_name": "read_file",
                        # Pinned verbatim from discovery. The description is
                        # inside the digest too: it is model-visible text an
                        # external process controls, so a server that rewrites
                        # it has changed what the agent was pinned to.
                        "description": "Read a file from the documentation share.",
                        "input_schema": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                            "required": ["path"],
                        },
                        # ``tool_schema_hash(name, description, input_schema)``
                        # at import time; the session pool re-computes it from
                        # the live server and refuses to call on a mismatch.
                        "schema_hash": "8f14e45fceea167a5a36dedd4bea2543" * 2,
                    },
                }
            ),
        ],
        edges=[
            Edge(
                edge_id="researcher->mcp_docs_search_read_file",
                source_node_id="researcher",
                target_node_id="mcp_docs_search_read_file",
                kind="tool",
            )
        ],
    )


async def _errors(*, node_caps: list[str], agent_caps: list[str]) -> list[str]:
    """Publish-time errors for one variant, via the real validator."""
    graph = build_imported_graph(node_caps=node_caps, agent_caps=agent_caps)
    report = await GraphValidator(mcp_grants_resolver=_grants_for).validate(graph)
    return [issue.message for issue in report.issues if issue.severity.value == "error"]


async def _report(label: str, *, node_caps: list[str], agent_caps: list[str]) -> None:
    errors = await _errors(node_caps=node_caps, agent_caps=agent_caps)
    verdict = "PUBLISHES" if not errors else "REFUSED"
    print(f"\n{label}\n  node declares : {node_caps}\n  agent holds   : {agent_caps}")
    print(f"  → {verdict}")
    for message in errors:
        print(f"    - {message}")


async def _run() -> int:
    granted = sorted(c.value for c in REGISTERED_GRANTS["docs-search"])
    print("Operator's registry row for 'docs-search':")
    print(f"  grants: {granted}")
    print("  command/args/env: operator-owned, never written by the graph author")

    await _report(
        "1. What `zeroth-core mcp-import` writes",
        node_caps=FLOOR,
        agent_caps=FLOOR,
    )
    await _report(
        "2. Ceiling — the node asks for more than the operator granted",
        node_caps=[*FLOOR, Capability.MEMORY_WRITE.value],
        agent_caps=[*FLOOR, Capability.MEMORY_WRITE.value],
    )
    await _report(
        "3. Agent floor — a hand-wired node the agent was never granted for",
        node_caps=FLOOR,
        agent_caps=[],
    )
    await _report(
        "4. The ceiling's subject is the NODE: the agent may hold more",
        node_caps=FLOOR,
        agent_caps=[*FLOOR, Capability.MEMORY_WRITE.value],
    )

    print(
        "\nDelivery: an mcp_tool call is at-least-once. It carries no operation\n"
        "identity, no receipt, no replay suppression and no reconciliation, so a\n"
        "retried agent turn calls the tool twice with nothing to suppress the\n"
        "duplicate. That is marked, not implied: the tool-call audit record sets\n"
        "operation_support=at_least_once and operation_residual_duplicate_risk=true.\n"
        "It is also why mcp_tool is its own node kind rather than a mode on\n"
        "ExecutableUnitNode, where the weaker guarantee would be invisible."
    )
    print(
        "\nStill open: the deprecated inline AgentNodeData.mcp_servers path publishes\n"
        "on a WARNING. It has no registry row and therefore no ceiling at all, and\n"
        "its tools are discovered at run time rather than pinned. While it exists,\n"
        "'grants is the one side an author cannot edit' is true of mcp_tool nodes\n"
        "and false of that path."
    )
    return 0


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
