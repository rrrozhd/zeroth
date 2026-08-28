"""Freeze a live MCP server's tools into a draft graph.

This is the step that makes an MCP tool governable. A server advertises its
tools at ``list_tools()`` on the day of the run, which is the opposite of what
the graph model assumes -- publish validation, diffing and version pinning all
need a contract that exists beforehand. Importing takes that contract once, at
design time, and freezes it as ``mcp_tool`` nodes the runtime later re-checks
the live server against.

The author never writes the server's command, args or env: those come from the
operator-owned registry row, and only its ``ref`` reaches the graph.

**Re-import is the documented repair, so it must be idempotent.**
``MCPSchemaDriftError`` tells the operator to "re-import to accept" and the
console repeats that advice, so importing the same tool twice is not an edge
case -- it is the supported way to accept a server that legitimately changed a
tool. Tools are therefore keyed by ``(server_ref, tool_name)`` and an existing
node is *updated in place*; suffixing a second node (``..._2``) and appending a
second binding of the same name produced a draft publish rejects with "tool
names must be unique per agent", which bricked every graph pinned to a server
that shipped an update.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from zeroth.contracts.graph.models import (
    AgentNode,
    AgentToolBinding,
    DisplayMetadata,
    Edge,
    GraphStatus,
    MCPToolNode,
    MCPToolNodeData,
)
from zeroth.integrations.mcp.config_repository import MCPServerConfigRepository
from zeroth.runtime.agents.mcp import (
    MCP_REQUIRED_CAPABILITIES,
    MCPClientManager,
    MCPServerConfig,
    MCPTimeoutError,
    tool_schema_hash,
)
from zeroth.runtime.agents.models import ToolOutputSafetyConfig
from zeroth.runtime.agents.sanitization import (
    ToolDeclarationSafetyError,
    screen_tool_declaration,
)

logger = logging.getLogger(__name__)

#: Capabilities every mcp_tool node needs, as ``capability_bindings`` refs.
#: Reaching a server spawns a subprocess that calls out, and publish, the runner
#: tool gate and the session pool all demand the pair -- so the import writes it
#: rather than leaving the author to discover the requirement from a validation
#: error. Derived from the one definition in ``runtime.agents.mcp`` so this
#: module cannot drift from the gates it is trying to satisfy.
_REQUIRED_REFS: tuple[str, ...] = tuple(
    sorted(capability.value for capability in MCP_REQUIRED_CAPABILITIES)
)


class MCPImportError(RuntimeError):
    """The import cannot produce a graph that would publish."""


@dataclass(frozen=True)
class ImportedTool:
    """One tool as it was pinned."""

    node_id: str
    tool_name: str
    schema_hash: str
    #: Injection heuristics the server's own declared prose matched. Flagged,
    #: never blocking -- see :func:`_screen_declaration`.
    declaration_flags: tuple[str, ...] = ()
    #: Whether this updated a node a previous import had already pinned.
    replaced: bool = False


@dataclass(frozen=True)
class _Discovered:
    """One advertised tool: the raw manifest, plus what screening made of it."""

    manifest: Any
    declaration_flags: tuple[str, ...]


async def import_mcp_tools(
    database: Any,
    graph_repository: Any,
    *,
    server_ref: str,
    graph_id: str,
    agent_node_id: str,
    tool_names: list[str] | None = None,
    tenant_id: str = "default",
) -> list[ImportedTool]:
    """Pin *server_ref*'s tools into *graph_id*, attached to *agent_node_id*.

    Raises rather than writing a partial graph: a draft that cannot publish is
    worse than no change, because the author discovers it later and further
    from the cause. Every refusal below is a case where the write would have
    produced exactly that.

    Running this twice for the same ``(server_ref, tool_name)`` re-pins the
    existing node instead of adding a second one -- see the module docstring.
    """
    registry = MCPServerConfigRepository(database)
    record = await registry.get(server_ref, tenant_id=tenant_id)
    if record is None:
        raise MCPImportError(
            f"MCP server {server_ref!r} is not registered; an operator must add it "
            "(POST /v1/mcp/servers) before a graph can reference it"
        )

    # The operator's ceiling, checked before the server is even started. Every
    # mcp_tool node must declare the required pair (the floor) and stay inside
    # the server's grants (the ceiling), so a server granting less than the pair
    # can have no publishable node at all. ``grants=[]`` is the default for a
    # newly registered server, which makes this the first wall a new operator
    # hits -- meeting it here, by name, beats meeting it as a validation error
    # against a draft that was already written.
    ungranted = sorted(
        capability.value for capability in (MCP_REQUIRED_CAPABILITIES - set(record.grants))
    )
    if ungranted:
        held = ", ".join(sorted(capability.value for capability in record.grants)) or "nothing"
        raise MCPImportError(
            f"MCP server {server_ref!r} grants {held}, but every mcp_tool node needs "
            f"{', '.join(_REQUIRED_REFS)}: reaching a server spawns a subprocess that "
            f"calls out. An operator must grant {', '.join(ungranted)} first -- "
            f"PUT /v1/mcp/servers/{server_ref} with "
            f'{{"grants": {list(_REQUIRED_REFS)}}}'
        )

    graph = await graph_repository.get(graph_id, tenant_id=tenant_id)
    if graph is None:
        raise MCPImportError(f"graph {graph_id!r} not found")
    if graph.status is not GraphStatus.DRAFT:
        raise MCPImportError(
            f"graph {graph_id!r} is {graph.status.value}, not draft; published versions are "
            "immutable -- clone it to a draft first"
        )

    agent = next(
        (n for n in graph.nodes if n.node_id == agent_node_id and isinstance(n, AgentNode)), None
    )
    if agent is None:
        raise MCPImportError(f"graph {graph_id!r} has no agent node {agent_node_id!r}")

    discovered = await _discover(record)
    by_name = {d.manifest.alias: d for d in discovered}
    wanted = tool_names or sorted(by_name)
    missing = [name for name in wanted if name not in by_name]
    if missing:
        raise MCPImportError(
            f"MCP server {server_ref!r} does not offer {', '.join(sorted(missing))}; "
            f"it offers {', '.join(sorted(by_name)) or '(nothing)'}"
        )

    # A binding name already spoken for by a different target would publish as
    # "tool names must be unique per agent" -- the same rejection the suffixing
    # re-import used to cause, reached from the other direction.
    bound_by_name = {b.name: b.target_node_id for b in agent.agent.tool_bindings}
    pinned = {
        (node.mcp_tool.server_ref, node.mcp_tool.tool_name): node
        for node in graph.nodes
        if isinstance(node, MCPToolNode)
    }
    clashes = sorted(
        name
        for name in wanted
        if name in bound_by_name
        and bound_by_name[name] != getattr(pinned.get((server_ref, name)), "node_id", None)
    )
    if clashes:
        raise MCPImportError(
            f"agent {agent_node_id!r} already binds a different tool named "
            f"{', '.join(clashes)}; rename that binding, or import the tool onto another "
            "agent -- tool names must be unique per agent"
        )

    existing_ids = {node.node_id for node in graph.nodes}
    imported: list[ImportedTool] = []
    replaced_nodes: dict[str, MCPToolNode] = {}
    new_nodes: list[MCPToolNode] = []
    new_edges: list[Edge] = []
    new_bindings: list[AgentToolBinding] = []
    # Two different questions, and publish asks the second one. An edge id is
    # unique graph-wide, so a tool edge that already exists must never be
    # written again -- but ``validate_tool_attachments`` only counts *enabled*
    # tool edges when it decides whether a binding is attached. An author who
    # disabled an edge made a decision the import does not overturn; it just
    # stops short of adding the binding that would then be rejected as pointing
    # at something unattached.
    edged = {
        edge.target_node_id
        for edge in graph.edges
        if edge.kind == "tool" and edge.source_node_id == agent_node_id
    }
    attached = {
        edge.target_node_id
        for edge in graph.edges
        if edge.kind == "tool" and edge.enabled and edge.source_node_id == agent_node_id
    }

    for name in wanted:
        entry = by_name[name]
        manifest = entry.manifest
        # The digest is taken over the RAW manifest, never the screened one: the
        # session pool re-hashes what the live server advertises, so hashing a
        # display transform here would make every pinned graph read as drifted.
        digest = tool_schema_hash(manifest.alias, manifest.description, manifest.parameters_schema)
        previous = pinned.get((server_ref, name))
        if previous is not None:
            # Re-pin in place. ``capability_bindings``, the binding and the edge
            # are deliberately untouched: they are author decisions, and the
            # server changing a schema says nothing about them.
            node_id = previous.node_id
            replaced_nodes[node_id] = previous.model_copy(
                update={
                    "display": previous.display.model_copy(update={"title": _display_name(name)}),
                    "mcp_tool": previous.mcp_tool.model_copy(
                        update={
                            "description": manifest.description,
                            "input_schema": manifest.parameters_schema or {},
                            "schema_hash": digest,
                        }
                    )
                }
            )
        else:
            node_id = _node_id(server_ref, name, existing_ids)
            existing_ids.add(node_id)
            new_nodes.append(
                MCPToolNode(
                    node_id=node_id,
                    graph_version_ref=agent.graph_version_ref,
                    display=DisplayMetadata(title=_display_name(name)),
                    capability_bindings=list(_REQUIRED_REFS),
                    mcp_tool=MCPToolNodeData(
                        server_ref=server_ref,
                        tool_name=name,
                        description=manifest.description,
                        input_schema=manifest.parameters_schema or {},
                        schema_hash=digest,
                    ),
                )
            )
        if node_id not in edged:
            new_edges.append(
                Edge(
                    edge_id=f"{agent_node_id}->{node_id}",
                    source_node_id=agent_node_id,
                    target_node_id=node_id,
                    kind="tool",
                )
            )
            edged.add(node_id)
            attached.add(node_id)
        if node_id in attached and node_id not in bound_by_name.values():
            new_bindings.append(
                AgentToolBinding(
                    target_node_id=node_id,
                    name=name,
                    # A tool binding requires a description, and the server's is
                    # the only one that exists at import. It is external text;
                    # the transform that keeps it out of the model's instruction
                    # surface is applied when the manifest is built, not here,
                    # so what the graph stores stays what the server said.
                    description=manifest.description or f"{name} on MCP server {server_ref}",
                )
            )
            bound_by_name[name] = node_id
        imported.append(
            ImportedTool(
                node_id=node_id,
                tool_name=name,
                schema_hash=digest,
                declaration_flags=entry.declaration_flags,
                replaced=previous is not None,
            )
        )

    agent.agent.tool_bindings = list(agent.agent.tool_bindings) + new_bindings
    # Clause (iii) of the capability predicate: publish and the runner tool gate
    # both require the *agent* to hold everything its bound tools declare, so an
    # import that wrote the pair onto the mcp_tool node alone produced a draft
    # that could not publish. Added rather than refused, because the point of
    # this command is a draft that publishes and runs without hand-editing JSON;
    # there is no ceiling on an agent's own bindings for this to widen past.
    agent.capability_bindings = list(agent.capability_bindings) + [
        ref for ref in _REQUIRED_REFS if ref not in agent.capability_bindings
    ]
    updated = graph.model_copy(
        update={
            "nodes": [replaced_nodes.get(n.node_id, n) for n in graph.nodes] + new_nodes,
            "edges": list(graph.edges) + new_edges,
        }
    )
    await graph_repository.save(updated, tenant_id=tenant_id)
    return imported


def _display_name(tool_name: str) -> str:
    """Turn a protocol identifier into the concise canvas label."""
    words = tool_name.replace("-", " ").replace("_", " ").split()
    return " ".join(words).capitalize() or tool_name


async def _discover(record: Any) -> list[_Discovered]:
    """Start the server, take its tool list, screen each declaration, stop it.

    Every way of failing to reach the server becomes an ``MCPImportError``: a
    command that is not on PATH, a command that is not an MCP server, and one
    that connects but never answers are the three most likely first runs, and
    each of them used to reach the operator as a raw traceback.
    """
    manager = MCPClientManager(
        [
            MCPServerConfig(
                name=record.ref,
                command=record.command,
                args=list(record.args),
                env=dict(record.env) or None,
            )
        ]
    )
    try:
        manifests = await manager.start()
    except MCPTimeoutError as exc:
        raise MCPImportError(
            f"MCP server {record.ref!r} did not answer within its deadline: {exc}. "
            f"Check that {record.command!r} speaks MCP over stdio"
        ) from exc
    except OSError as exc:
        raise MCPImportError(
            f"MCP server {record.ref!r} could not be started: {record.command!r}: {exc}. "
            "The registered command must be an executable on this host's PATH"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - any discovery failure is an import failure
        raise MCPImportError(
            f"MCP server {record.ref!r} failed to advertise its tools: "
            f"{type(exc).__name__}: {exc}. Check that {record.command!r} is an MCP server"
        ) from exc
    finally:
        await manager.stop()
    return [_screen_declaration(record.ref, manifest) for manifest in manifests]


def _screen_declaration(server_ref: str, manifest: Any) -> _Discovered:
    """Put one advertised declaration through the model-boundary screen.

    A tool's description and the prose inside its schema are text an external
    process chose, and they land in the model's instruction surface on every
    step. Running the same transform the runtime applies means an import can
    say something about that text -- and it is what makes the
    ``ToolDeclarationSafetyError`` branch below reachable at all: it is raised
    by screening, never by ``MCPClientManager.start``, which is why catching it
    around discovery alone was dead code.

    Flags do not block, matching ``screen_tool_description``'s documented
    posture: the heuristics are conservative and refusing a tool on a heuristic
    match would silently remove a legitimate capability. Only a declaration
    whose *bounds* cannot be represented -- one screening itself refuses --
    fails the import.

    The screened manifest is deliberately discarded. It exists here to be
    checked and reported; what gets pinned is the raw manifest, because the
    session pool hashes the live server's raw text and a pin taken over a
    display transform would read as permanent drift.

    The default ``ToolOutputSafetyConfig`` is used because there is no other
    one: the setting lives on the agent *runner*, not on ``AgentNodeData``, so
    nothing in the graph can answer it at design time. That also means an
    operator who turns screening off at run time still gets this report, which
    is the right way round for a design-time command.
    """
    try:
        screened = screen_tool_declaration(manifest, ToolOutputSafetyConfig())
    except ToolDeclarationSafetyError as exc:
        # Screened here rather than skipped at run time: a tool whose
        # declaration cannot be bounded should fail the import, where an author
        # is watching, not vanish silently from a running agent's toolset.
        raise MCPImportError(
            f"MCP server {server_ref!r} advertises a tool whose declaration cannot be "
            f"bounded safely: {exc}"
        ) from exc
    audit = screened.metadata.get("tool_description_safety") or {}
    flags = tuple(audit.get("flags") or ())
    if flags:
        logger.warning(
            "MCP tool %s on server %s declares text matching injection heuristics: %s",
            manifest.alias,
            server_ref,
            ",".join(flags),
        )
    return _Discovered(manifest=manifest, declaration_flags=flags)


def _node_id(server_ref: str, tool_name: str, taken: set[str]) -> str:
    """A readable, collision-free node id.

    The suffix is only ever reached by a *different* node already owning the
    readable name -- a re-import of the same ``(server_ref, tool_name)`` updates
    that node in place and never lands here.
    """
    base = f"mcp_{server_ref}_{tool_name}".replace("-", "_")
    if base not in taken:
        return base
    suffix = 2
    while f"{base}_{suffix}" in taken:
        suffix += 1
    return f"{base}_{suffix}"


__all__ = ["ImportedTool", "MCPImportError", "import_mcp_tools"]
