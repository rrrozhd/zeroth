"""Publish-time rules for the ``mcp_tool`` node.

The node exists so an MCP tool has a contract *before* the run. These tests
pin the rules that make that true: it must be pinned, it must stay inside the
operator's ceiling, the agent that binds it must hold what the runner gate will
demand of it, it must not pretend to carry a registered contract, and it must
not be wired into control flow.

Nothing in this file spells the required capability pair as a literal. Publish
and the session pool disagreeing about one node is the defect these tests
exist to catch, and four independent copies of the pair agreeing by habit is
how that disagreement got in: import
:data:`~zeroth.runtime.agents.mcp.MCP_REQUIRED_CAPABILITIES` instead.
"""

from __future__ import annotations

import ast
import importlib
import inspect

import pytest

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
from zeroth.contracts.graph.validation_errors import ValidationCode, ValidationIssue
from zeroth.runtime.agents.mcp import MCP_REQUIRED_CAPABILITIES
from zeroth.runtime.graph_validation import GraphValidator

#: The floor as an author writes it in ``capability_bindings``.
_REQUIRED_REFS = sorted(capability.value for capability in MCP_REQUIRED_CAPABILITIES)

_GRANTS: dict[str, set[Capability]] = {
    "filesystem": set(MCP_REQUIRED_CAPABILITIES) | {Capability.FILESYSTEM_READ},
    "locked_down": set(),
}


async def _resolve(server_ref: str) -> set[Capability] | None:
    return _GRANTS.get(server_ref)


def _graph(**overrides) -> Graph:
    node: dict = {
        "node_id": "fs_read",
        "graph_version_ref": "g:v1",
        "node_type": "mcp_tool",
        "capability_bindings": list(_REQUIRED_REFS),
        "mcp_tool": {
            "server_ref": "filesystem",
            "tool_name": "read_file",
            "description": "Read a file",
            "input_schema": {"type": "object"},
            "schema_hash": "a" * 64,
        },
    }
    node.update(overrides)
    return Graph.model_validate(
        {
            "graph_id": "g",
            "name": "g",
            "version": 1,
            # The entry step is the entrypoint node and not ``fs_read``, which is
            # what it used to be. An ``mcp_tool`` node is reached only through a
            # tool edge, so entering there fails on the run's first dispatch --
            # see ``test_an_mcp_tool_node_cannot_be_the_entry_step``. The
            # entrypoint adds no errors of its own, so every grants assertion
            # below still sees exactly the issues its own rule produced.
            "entry_step": "start",
            # The pinned node stays at index 0: several tests below reach for
            # ``graph.nodes[0].mcp_tool`` directly.
            "nodes": [
                node,
                {
                    "node_id": "start",
                    "graph_version_ref": "g:v1",
                    "node_type": "entrypoint",
                    "input_contract_ref": "contract://in",
                    "output_contract_ref": "contract://out",
                    "entrypoint": {},
                },
            ],
            "edges": [],
        }
    )


async def _errors(graph: Graph, *, resolver=_resolve) -> list[str]:
    report = await GraphValidator(mcp_grants_resolver=resolver).validate(graph)
    return [issue.message for issue in report.issues if issue.severity.value == "error"]


@pytest.mark.asyncio
async def test_a_pinned_node_inside_its_ceiling_validates() -> None:
    assert await _errors(_graph()) == []


@pytest.mark.asyncio
async def test_declaring_more_than_the_server_grants_is_rejected() -> None:
    """The check the registry exists for.

    ``capability_bindings`` are author-declared, so the server's grants are the
    only side of this comparison the graph author cannot edit.
    """
    errors = await _errors(_graph(capability_bindings=[*_REQUIRED_REFS, "memory_write"]))
    assert any("memory_write" in message for message in errors)


@pytest.mark.asyncio
async def test_a_server_granting_nothing_denies_a_node_that_declares_something() -> None:
    """Half of the "empty grants deny everything" claim.

    On its own this was a false witness: it only ever showed that a node
    declaring capabilities exceeds an empty ceiling. The harder half -- a node
    declaring *nothing* -- used to pass, because an empty declaration exceeds
    nothing. The companion test below covers it.
    """
    graph = _graph(capability_bindings=list(_REQUIRED_REFS))
    graph.nodes[0].mcp_tool.server_ref = "locked_down"
    errors = await _errors(graph)
    assert any("does not grant" in message for message in errors)


@pytest.mark.asyncio
async def test_an_unregistered_server_ref_is_rejected() -> None:
    graph = _graph()
    graph.nodes[0].mcp_tool.server_ref = "never-registered"
    assert any("unknown MCP server" in message for message in await _errors(graph))


@pytest.mark.asyncio
async def test_an_unpinned_node_is_rejected() -> None:
    """Without a schema_hash the node names a tool of unknown shape."""
    graph = _graph()
    graph.nodes[0].mcp_tool.schema_hash = "   "
    assert any("schema_hash" in message for message in await _errors(graph))


@pytest.mark.asyncio
async def test_a_contract_ref_is_rejected_rather_than_ignored() -> None:
    """Silently ignoring an author's ref is how a governance surface starts lying."""
    errors = await _errors(_graph(input_contract_ref="contracts/thing"))
    assert any("must not set input_contract_ref" in message for message in errors)


@pytest.mark.asyncio
async def test_contract_refs_are_not_required_either() -> None:
    """The pinned input_schema is the contract; requiring both rules at once
    would make the node type impossible to satisfy."""
    assert not any("contract ref is required" in message for message in await _errors(_graph()))


@pytest.mark.asyncio
async def test_without_a_resolver_the_ceiling_pass_is_skipped() -> None:
    """Contract-only callers have no deployment to resolve a ref against.

    This documents a FAIL-OPEN posture, so on its own it is a hazard, not a
    reassurance: an unwired validator accepts any capability an author writes.
    It is only safe because ``TestTheCeilingIsWiredWherePublishHappens`` below
    pins that the publishing path is wired, and that it is wired with a real
    resolver rather than with ``None``. Do not read this test as saying the skip
    is harmless.
    """
    graph = _graph(capability_bindings=[*_REQUIRED_REFS, "memory_write"])
    report = await GraphValidator().validate(graph)
    assert [i for i in report.issues if "does not grant" in i.message] == []


class TestTheCeilingIsWiredWherePublishHappens:
    """The publishing path must pass a real grants resolver.

    The ceiling is compared in exactly one place, and that place returns early
    when the resolver is None. A validator built without it therefore does not
    merely skip a check -- it makes the operator-owned registry decorative,
    while every test above keeps passing.

    A name-only check is not enough, because ``mcp_grants_resolver=None``
    passes the keyword and still disarms the pass. So the detector rejects two
    arrangements, and the meta-tests below feed it each of them plus the wired
    shape it must accept: a guard that rejects everything proves as little as
    one that accepts everything.
    """

    @staticmethod
    def _constructions(source: str) -> list[ast.Call]:
        return [
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "GraphValidator"
        ]

    @classmethod
    def _unwired(cls, source: str) -> list[str]:
        """Why each ``GraphValidator(...)`` in *source* leaves the ceiling off."""
        reasons: list[str] = []
        for call in cls._constructions(source):
            passed = {kw.arg: kw.value for kw in call.keywords}
            resolver = passed.get("mcp_grants_resolver")
            if resolver is None:
                reasons.append("mcp_grants_resolver was not passed")
            elif isinstance(resolver, ast.Constant) and resolver.value is None:
                reasons.append("mcp_grants_resolver=None disarms the ceiling pass")
        return reasons

    @pytest.mark.parametrize(
        "module_name",
        # Every module that builds a validator a graph is *published* through,
        # not just the one the service boots. ``demo.py`` is a second publish
        # path with its own construction, and a guard aimed at one of them
        # leaves the None-resolver hole open on the other -- which is how a
        # decorative registry ships under a green suite.
        ["zeroth.service.bootstrap.factory", "zeroth.service.demo"],
    )
    def test_every_publish_path_wires_a_resolver(self, module_name: str) -> None:
        source = inspect.getsource(importlib.import_module(module_name))
        assert self._constructions(source), (
            f"no GraphValidator construction found in {module_name} -- "
            "this guard has gone vacuous"
        )
        assert self._unwired(source) == []

    def test_the_guard_would_notice_a_missing_keyword(self) -> None:
        assert self._unwired("GraphValidator(contract_registry=registry)") == [
            "mcp_grants_resolver was not passed"
        ]

    def test_the_guard_would_notice_an_explicit_none(self) -> None:
        """The arrangement the earlier name-only version of this guard accepted."""
        reasons = self._unwired("GraphValidator(mcp_grants_resolver=None)")
        assert reasons == ["mcp_grants_resolver=None disarms the ceiling pass"]

    def test_the_guard_accepts_a_real_resolver(self) -> None:
        assert self._unwired("GraphValidator(mcp_grants_resolver=self._mcp_grants)") == []


def test_the_node_survives_a_graph_round_trip() -> None:
    """Discriminated-union dispatch has to land on the new type, not a fallback."""
    restored = Graph.model_validate(_graph().model_dump(mode="json")).nodes[0]
    assert isinstance(restored, MCPToolNode)
    assert restored.to_governed_step_spec().tool["kind"] == "mcp_tool_ref"


@pytest.mark.asyncio
async def test_a_node_declaring_nothing_is_rejected_by_the_floor() -> None:
    """The half the ceiling alone could never catch.

    Declaring nothing exceeds nothing, so a pure ceiling made an empty
    declaration the cheapest way past publish -- and then MCPSessionPool denied
    it at dispatch, leaving publish and runtime disagreeing about one node.
    """
    errors = await _errors(_graph(capability_bindings=[]))
    assert any("is missing" in message for message in errors)


@pytest.mark.asyncio
async def test_a_server_granting_nothing_really_does_deny_every_node() -> None:
    """Now the documented claim holds for a node declaring nothing too.

    The floor demands the spawn pair, and an empty ceiling cannot cover it, so
    there is no declaration that satisfies both.
    """
    for bindings in ([], list(_REQUIRED_REFS)):
        graph = _graph(capability_bindings=bindings)
        graph.nodes[0].mcp_tool.server_ref = "locked_down"
        assert await _errors(graph), f"{bindings!r} slipped past an empty ceiling"


@pytest.mark.asyncio
@pytest.mark.parametrize("withheld", sorted(MCP_REQUIRED_CAPABILITIES, key=lambda c: c.value))
async def test_publish_demands_exactly_what_the_pool_demands(withheld: Capability) -> None:
    """Publish and runtime must not disagree about the same node.

    MCPSessionPool requires the whole floor before handing out a session; if
    publish asked for any less, an accepted graph would fail at dispatch
    instead. Parametrised over the constant rather than naming one capability,
    so a capability added to the floor is covered here the moment it is added.
    """
    declared = [ref for ref in _REQUIRED_REFS if ref != withheld.value]
    errors = await _errors(_graph(capability_bindings=declared))
    assert any(withheld.value in message for message in errors)


# --------------------------------------------------------------------------
# The agent floor: what ``zeroth-core mcp-import`` produces must not publish
# until the agent itself can be granted what the runner gate will demand.
# --------------------------------------------------------------------------


def _imported_graph(*, agent_caps: list[str]) -> Graph:
    """The exact shape ``mcp-import`` writes, parametrised by the agent's grant.

    ``service/mcp_import.py`` appends an ``MCPToolNode`` carrying the floor, a
    ``kind="tool"`` edge, and an ``AgentToolBinding`` with no
    ``required_capabilities`` -- and never touches the agent's own
    ``capability_bindings``. So ``agent_caps=[]`` is the CLI's own output,
    verbatim.
    """
    return Graph(
        graph_id="imported",
        name="imported",
        version=1,
        entry_step="agent",
        execution_settings=ExecutionSettings(max_total_steps=5),
        nodes=[
            AgentNode(
                node_id="agent",
                graph_version_ref="imported@1",
                input_contract_ref="contract://in",
                output_contract_ref="contract://out",
                capability_bindings=agent_caps,
                agent=AgentNodeData(
                    instruction="use the tool",
                    model_provider="governai:test",
                    tool_bindings=[
                        AgentToolBinding(
                            target_node_id="fs_read",
                            name="read_file",
                            description="Read a file",
                        )
                    ],
                ),
            ),
            MCPToolNode.model_validate(
                {
                    "node_id": "fs_read",
                    "graph_version_ref": "imported@1",
                    "node_type": "mcp_tool",
                    "capability_bindings": list(_REQUIRED_REFS),
                    "mcp_tool": {
                        "server_ref": "filesystem",
                        "tool_name": "read_file",
                        "description": "Read a file",
                        "input_schema": {"type": "object"},
                        "schema_hash": "a" * 64,
                    },
                }
            ),
        ],
        edges=[
            Edge(
                edge_id="agent->fs_read",
                source_node_id="agent",
                target_node_id="fs_read",
                kind="tool",
            )
        ],
    )


async def _issues(graph: Graph) -> list[ValidationIssue]:
    """Like ``_errors`` but keeps codes and details, which these rules assert on."""
    report = await GraphValidator(mcp_grants_resolver=_resolve).validate(graph)
    return list(report.issues)


@pytest.mark.asyncio
async def test_the_import_shape_does_not_publish_until_the_agent_holds_the_floor() -> None:
    """The CLI's own output used to publish with zero errors and then be denied.

    ``tool_required_capabilities`` unions an ``mcp_tool`` target's
    ``capability_bindings`` into what the runner gate demands of the agent.
    Publish unioned them only for an ``ExecutableUnitNode``, so the pair the
    import writes onto the tool node was invisible here -- the agent was never
    asked to hold it, and the run died at the first tool call.
    """
    issues = await _issues(_imported_graph(agent_caps=[]))
    insufficient = [
        issue for issue in issues if issue.code == ValidationCode.CAPABILITY_GRANT_INSUFFICIENT
    ]
    assert len(insufficient) == 1, [issue.message for issue in issues]
    assert insufficient[0].node_id == "agent"
    assert insufficient[0].details["missing_capabilities"] == _REQUIRED_REFS


@pytest.mark.asyncio
async def test_granting_the_agent_the_floor_lets_the_import_shape_publish() -> None:
    """The other half: the rule must be satisfiable, not merely strict.

    Without this, the test above would pass just as well against a validator
    that rejected every agent binding an mcp_tool node.
    """
    assert await _errors(_imported_graph(agent_caps=list(_REQUIRED_REFS))) == []


@pytest.mark.asyncio
async def test_the_agent_floor_covers_a_capability_beyond_the_pair() -> None:
    """The rule is "cover the tool node", not "hold the two spawn capabilities".

    A node may declare anything its server grants; the agent has to cover that
    too, exactly as the runner gate will.
    """
    graph = _imported_graph(agent_caps=list(_REQUIRED_REFS))
    graph.nodes[1].capability_bindings = [*_REQUIRED_REFS, "filesystem_read"]
    issues = await _issues(graph)
    insufficient = [
        issue for issue in issues if issue.code == ValidationCode.CAPABILITY_GRANT_INSUFFICIENT
    ]
    assert len(insufficient) == 1
    assert insufficient[0].details["missing_capabilities"] == ["filesystem_read"]


# --------------------------------------------------------------------------
# Control flow: an mcp_tool node is a tool target, never a routed-to step.
# --------------------------------------------------------------------------


def _also_routed(*, reverse: bool, enabled: bool = True) -> Graph:
    """The valid import shape plus one control-flow edge touching the tool node.

    Added rather than substituted, so the tool edge and its binding stay intact
    and the routing edge is the only thing left to object to.
    """
    graph = _imported_graph(agent_caps=list(_REQUIRED_REFS))
    source, target = ("fs_read", "agent") if reverse else ("agent", "fs_read")
    return graph.model_copy(
        update={
            "edges": [
                *graph.edges,
                Edge(
                    edge_id="routed",
                    source_node_id=source,
                    target_node_id=target,
                    kind="data",
                    enabled=enabled,
                ),
            ]
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("reverse", "role"), [(False, "target"), (True, "source")])
async def test_a_data_edge_touching_an_mcp_tool_node_is_rejected(reverse: bool, role: str) -> None:
    """It used to publish with an error set identical to ``edges: []``.

    The dispatcher has no ``MCPToolNode`` branch, so routing to one raises
    ``unsupported node type`` mid-run -- publish and dispatch disagreeing about
    the same graph, which is the thing this node type is supposed to prevent.

    The rule has to discriminate on the edge's *kind*: this graph minus the
    routing edge is the tool edge ``mcp-import`` writes, and
    ``test_granting_the_agent_the_floor_lets_the_import_shape_publish`` above
    pins that it still validates clean.
    """
    issues = await _issues(_also_routed(reverse=reverse))
    errors = [issue for issue in issues if issue.severity.value == "error"]
    assert len(errors) == 1, [issue.message for issue in errors]
    assert errors[0].code == ValidationCode.INVALID_NODE_ATTACHMENT
    assert errors[0].edge_id == "routed"
    assert errors[0].details == {"node_id": "fs_read", "edge_role": role}


@pytest.mark.asyncio
async def test_an_mcp_tool_node_cannot_be_the_entry_step() -> None:
    """Same disagreement as the data edge, reached through the other door.

    ``NodeDispatcher.dispatch`` branches on agent, entrypoint, executable-unit
    and retrieval nodes and raises ``unsupported node type`` for anything else,
    so a run entering at an ``mcp_tool`` node dies on its first hop. Found by
    driving the console: the Studio entry-step picker offers every node in the
    graph. The canvas itself is safe -- the Studio route normalises
    ``entry_step`` onto the entrypoint node, and a probe against the live API
    confirmed the value is rewritten before it reaches publish -- so this is
    reachable only through the API or a code-authored graph, exactly the
    wider-than-the-canvas surface the data-edge rule covers.
    """
    graph = _imported_graph(agent_caps=list(_REQUIRED_REFS)).model_copy(
        update={"entry_step": "fs_read"}
    )
    issues = await _issues(graph)
    errors = [issue for issue in issues if issue.severity.value == "error"]
    assert len(errors) == 1, [issue.message for issue in errors]
    assert errors[0].code == ValidationCode.UNKNOWN_ENTRYPOINT
    assert errors[0].details == {"entry_step": "fs_read", "node_type": "mcp_tool"}


@pytest.mark.asyncio
async def test_the_agent_is_still_a_valid_entry_step() -> None:
    """Control for the rule above, which otherwise reads as "reject an entry step".

    ``_imported_graph`` enters at the agent, which is the shape every other test
    in this module publishes, so a rule that discriminated on the wrong thing
    would take the whole file down with it rather than fail here alone.
    """
    assert await _errors(_imported_graph(agent_caps=list(_REQUIRED_REFS))) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True])
async def test_a_disabled_data_edge_is_left_alone(reverse: bool) -> None:
    """The claim is about dispatch reachability, and disabled edges never dispatch.

    Every traversal on the execution path filters ``edge.enabled``, so rejecting
    a disabled edge would block publish on an edge the author already switched
    off -- clearable only by deleting it. Control for the rule above: without
    this, "reject a data edge touching an mcp_tool node" reads as unconditional.
    """
    assert await _errors(_also_routed(reverse=reverse, enabled=False)) == []
