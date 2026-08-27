"""The joint: an ``mcp_tool`` node driven through the orchestrator, gate live.

``RuntimeToolExecutor.build``'s ``mcp_tool`` branch is the only code that
dispatches one of these nodes at run time, and a ``sys.settrace`` line trace over
623 tests -- every MCP test file plus the orchestration, dispatch and
operation-identity suites -- recorded **zero** hits on it. The ``isinstance``
check above it ran and was always False. So the "no pool wired" refusal, the
argument mapping, the choice of which node id names which subject and the
capability threading were each proved in isolation and never together; the joint
was held up by AST guards reading source text, which is the same seam the
"shipped unwired" incident was about.

What separates this file from ``tests/test_mcp_integration.py``'s end-to-end
class is the **policy guard**. That one wires none, so
``effective_capabilities`` arrives at the pool as ``None`` and the agent floor
is advisory: nothing there can tell an enforced deny from an unenforced pass.
Every run below goes through a real :class:`PolicyGuard`, which is what makes
the two subjects distinguishable at all -- the ceiling is measured against the
``mcp_tool`` node's declared capabilities while the floor is measured against
what the guard granted the *agent*, and only a live guard puts a non-``None``
set on the second side of that comparison.

Two tests here are labelled REGRESSION GUARD. They cover behaviour that is
correct at ``HEAD`` and was simply never executed; they cannot fail against the
pre-fix tree, and saying so is the point -- an unexecuted branch is exactly what
this file exists to start executing.
"""

from __future__ import annotations

import logging
import sys
from typing import Any
from unittest.mock import patch

from pydantic import BaseModel

from tests.conftest import content_capture
from zeroth.contracts.graph import AgentNodeData, AgentToolBinding, Edge, Graph
from zeroth.contracts.graph.models import AgentNode, MCPToolNode, MCPToolNodeData
from zeroth.contracts.registry import ContractRegistry
from zeroth.governance.audit import AuditRepository
from zeroth.governance.policy import Capability, CapabilityRegistry, PolicyGuard
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.runtime.agents import DeterministicProviderAdapter, ProviderResponse
from zeroth.runtime.agents.factory import build_agent_runners
from zeroth.runtime.agents.mcp import (
    MCPClientManager,
    MCPServerConfig,
    RegisteredMCPServerConfig,
    tool_schema_hash,
)
from zeroth.runtime.agents.mcp_pool import MCPSessionPool
from zeroth.runtime.orchestration import RuntimeOrchestrator
from zeroth.runtime.runs import RunStatus

_SPAWN = [Capability.PROCESS_SPAWN, Capability.EXTERNAL_API_CALL]
_FIXTURE = ["-m", "tests.runtime.mcp_fixtures.echo_server"]

#: Pinned declarations, read from the live server once per session. Spawning the
#: fixture costs a process and a handshake, and every test below needs the same
#: two pins; taking them once keeps this file's process count proportional to
#: the runs it makes rather than to the assertions it writes.
_PINS: dict[str, tuple[str, dict[str, Any], str]] = {}


class EchoInput(BaseModel):
    text: str


class EchoOutput(BaseModel):
    echoed: str


class _NoExecutableUnits:
    """The unit runner an ``mcp_tool`` call must never reach.

    ``RuntimeOrchestrator`` requires one, and an inert stand-in would let a
    regression that routed an MCP call through the executable-unit path pass
    quietly. Raising makes that misrouting a failure with a name on it.
    """

    async def run(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("an mcp_tool call must not reach the executable-unit runner")


def _config(grants: list[Capability]) -> RegisteredMCPServerConfig:
    """The operator's registration: command, args and the ceiling."""
    return RegisteredMCPServerConfig(
        name="echo",
        command=sys.executable,
        args=list(_FIXTURE),
        grants=list(grants),
    )


def _resolver(grants: list[Capability]):
    async def resolve(server_ref: str) -> RegisteredMCPServerConfig | None:
        return _config(grants) if server_ref == "echo" else None

    return resolve


async def _pin(tool_name: str) -> tuple[str, dict[str, Any], str]:
    """Read a live declaration and freeze it exactly as an import would.

    Taken from the server rather than hand-written, because the digest the pool
    recomputes at run time is the one the server's own advertisement produces: a
    hand-written schema that merely looks right fails the drift check, and the
    test would then prove nothing about the round trip it claims to make.
    """
    cached = _PINS.get(tool_name)
    if cached is not None:
        return cached
    manager = MCPClientManager([_config(_SPAWN)])
    try:
        manifests = await manager.start()
    finally:
        await manager.stop()
    manifest = next(m for m in manifests if m.alias == tool_name)
    pin = (
        manifest.description,
        dict(manifest.parameters_schema or {}),
        tool_schema_hash(manifest.alias, manifest.description, manifest.parameters_schema),
    )
    _PINS[tool_name] = pin
    return pin


async def _graph(
    *,
    agent_capabilities: list[Capability],
    tools: list[tuple[str, str, list[Capability]]],
) -> Graph:
    """One agent joined by tool edges to one ``mcp_tool`` node per entry in *tools*.

    Each entry is ``(node_id, tool_name, declared_capabilities)``. The refs are
    written as capability *values* because that is what the three sites that
    read them accept: ``tool_required_capabilities`` and the executor's
    ``_declared_capabilities`` both call ``Capability(ref)`` and silently drop
    anything else, so a ``capability://`` style ref here would resolve to an
    empty declared set and every ceiling assertion in this file would pass
    against nothing.
    """
    nodes: list[Any] = []
    edges: list[Edge] = []
    bindings: list[AgentToolBinding] = []
    for index, (node_id, tool_name, declared) in enumerate(tools):
        description, schema, schema_hash = await _pin(tool_name)
        bindings.append(
            AgentToolBinding(
                target_node_id=node_id,
                name=tool_name,
                # What ``import_mcp_tools`` writes: the server's own text,
                # verbatim, with no ``arguments`` of its own.
                description=description,
            )
        )
        nodes.append(
            MCPToolNode(
                node_id=node_id,
                graph_version_ref="graph-mcp-e2e:v1",
                capability_bindings=[capability.value for capability in declared],
                mcp_tool=MCPToolNodeData(
                    server_ref="echo",
                    tool_name=tool_name,
                    description=description,
                    input_schema=schema,
                    schema_hash=schema_hash,
                ),
            )
        )
        edges.append(
            Edge(
                edge_id=f"tool-{index}",
                source_node_id="agent",
                target_node_id=node_id,
                kind="tool",
            )
        )
    agent = AgentNode(
        node_id="agent",
        graph_version_ref="graph-mcp-e2e:v1",
        capability_bindings=[capability.value for capability in agent_capabilities],
        input_contract_ref="contract://echo-in",
        output_contract_ref="contract://echo-out",
        agent=AgentNodeData(
            instruction="use your tools",
            model_provider="provider://test",
            tool_bindings=bindings,
        ),
    )
    return Graph(
        graph_id="graph-mcp-e2e",
        name="mcp-e2e",
        entry_step="agent",
        nodes=[agent, *nodes],
        edges=edges,
    )


def _guard() -> PolicyGuard:
    """A real guard whose registry answers the value refs the graph writes.

    Every capability is registered under its own value so an unregistered ref
    can never fail a run for a reason unrelated to the claim under test --
    ``CapabilityRegistry.resolve`` raises ``KeyError`` on a miss, which would
    read as a policy denial rather than as the harness gap it is.
    """
    registry = CapabilityRegistry()
    for capability in Capability:
        registry.register(capability.value, capability)
    return PolicyGuard(capability_registry=registry)


class _RecordingPool(MCPSessionPool):
    """The real pool, plus a note of what each call was asked to enforce.

    Subclassed rather than replaced: the gate, the spawn, the pin check and the
    transport all still run. The point is to read the arguments the dispatch
    seam actually passed -- mocking the pool would re-create the exact hole this
    file exists to close, since the seam's argument mapping is the thing under
    test.
    """

    def __init__(self, resolver: Any) -> None:
        super().__init__(resolver)
        self.calls: list[dict[str, Any]] = []

    async def call(self, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        return await super().call(**kwargs)


class _UnstoppablePool(_RecordingPool):
    """A pool whose teardown fails, after a run that otherwise succeeded.

    Not hypothetical: closing a session from a task other than the one that
    entered it raises out of ``stop`` with anyio's "exit cancel scope in a
    different task", which is what every parallel run used to hit.
    """

    async def stop(self) -> None:
        await super().stop()
        raise RuntimeError("server would not stop")


async def _run(
    sqlite_db: Any,
    graph: Graph,
    responses: list[ProviderResponse],
    *,
    pools: list[_RecordingPool],
    grants: list[Capability] | None = None,
    wire_resolver: bool = True,
    pool_class: type[_RecordingPool] = _RecordingPool,
) -> tuple[Any, DeterministicProviderAdapter, list[Any]]:
    """Drive *graph* through a real orchestrator with capability enforcement on.

    ``wire_resolver=False`` reproduces a deployment with no MCP registry: the
    orchestrator then owns no pool for the run, which is the arrangement the
    executor must fail closed on.
    """
    contract_registry = ContractRegistry.for_default_compatibility(sqlite_db)
    await contract_registry.register(EchoInput, name="contract://echo-in")
    await contract_registry.register(EchoOutput, name="contract://echo-out")

    provider = DeterministicProviderAdapter(responses)
    runners = await build_agent_runners(graph, contract_registry, provider=provider)

    def build_pool(resolver: Any) -> _RecordingPool:
        pool = pool_class(resolver)
        pools.append(pool)
        return pool

    orchestrator = RuntimeOrchestrator(
        audit_repository=content_capture(AuditRepository.for_default_compatibility(sqlite_db)),
        run_repository=RunRepository.for_default_compatibility(sqlite_db),
        agent_runners=runners,
        executable_unit_runner=_NoExecutableUnits(),
        policy_guard=_guard(),
        mcp_server_resolver=(
            _resolver(_SPAWN if grants is None else grants) if wire_resolver else None
        ),
    )
    with patch(
        "zeroth.runtime.orchestration.orchestrator.MCPSessionPool",
        side_effect=build_pool,
    ):
        run = await orchestrator.run_graph(graph, {"text": "hello"})
    audits = await AuditRepository.for_default_compatibility(sqlite_db).list_by_run(run.run_id)
    return run, provider, audits


def _tool_calls(audits: list[Any]) -> list[Any]:
    """Every tool call the run recorded, in order."""
    return [call for audit in audits for call in audit.tool_calls]


def _echo_then_answer(*, answer: str = "hello") -> list[ProviderResponse]:
    """A provider that asks for ``echo`` once, then answers."""
    return [
        ProviderResponse(
            content=None,
            tool_calls=[{"id": "call-1", "name": "echo", "args": {"text": "hello"}}],
        ),
        ProviderResponse(content=f'{{"echoed": "{answer}"}}'),
    ]


class TestPinnedMCPToolNodeUnderLiveEnforcement:
    """Findings 2, 8, 9 and 11, observed through one dispatch path."""

    async def test_the_call_reaches_the_server_and_the_pool_is_told_which_subject_is_which(
        self, sqlite_db
    ):
        """A real round trip, and the two ids the pool was handed for it.

        The subjects are the claim. ``tool_node_id`` keys the operator's ceiling
        and ``agent_node_id`` names the floor's holder; one ``node_id``
        parameter serving both is what let the ceiling be measured against the
        agent, and nothing observed the dispatch seam's choice until now.
        """
        pools: list[_RecordingPool] = []
        graph = await _graph(
            agent_capabilities=_SPAWN,
            tools=[("mcp_echo", "echo", _SPAWN)],
        )
        run, _provider, audits = await _run(sqlite_db, graph, _echo_then_answer(), pools=pools)

        assert run.status is RunStatus.COMPLETED
        assert run.final_output == {"echoed": "hello"}

        (recorded,) = pools[0].calls
        assert recorded["tool_node_id"] == "mcp_echo"
        assert recorded["agent_node_id"] == "agent"
        assert recorded["server_ref"] == "echo"
        # Non-emptiness is asserted separately: a capability ref the executor
        # cannot resolve is dropped rather than raised, so an empty set would
        # satisfy the equality below against a graph that declared nothing.
        assert recorded["declared_capabilities"], recorded
        assert recorded["declared_capabilities"] == set(_SPAWN)
        assert recorded["arguments"] == {"text": "hello"}
        assert recorded["pinned_hash"] == graph.nodes[1].mcp_tool.schema_hash
        # Capability enforcement is LIVE for this run, which is what makes the
        # floor a gate rather than a comment. ``None`` here would mean the guard
        # was never wired and every capability claim in this file was advisory.
        assert recorded["effective_capabilities"] == set(_SPAWN)

        (tool_call,) = _tool_calls(audits)
        assert tool_call.tool_ref == "node://mcp_echo"
        assert tool_call.error is None
        # The echo server returned the text it was given: the call crossed a
        # real pipe rather than being satisfied inside the runtime.
        assert tool_call.outcome == {"value": "hello"}
        # A successful MCP call still carries the weaker guarantee.
        assert tool_call.operation_support == "at_least_once"
        assert tool_call.operation_residual_duplicate_risk is True

    async def test_an_agents_unrelated_capabilities_are_not_measured_against_the_grants(
        self, sqlite_db
    ):
        """Finding 8: the ceiling's subject is the node, so ``memory_read`` is none of it.

        The agent holds ``memory_read`` for reasons that have nothing to do with
        this server -- memory bindings elsewhere in its own definition. Measuring
        *that* set against the server's grants demanded the operator grant an MCP
        server a capability the server never touches, and following the error
        message converges every server's grants on the union of everything any
        agent holds. The node declares exactly what the attachment does, and that
        is what the grants bound.
        """
        pools: list[_RecordingPool] = []
        graph = await _graph(
            agent_capabilities=[*_SPAWN, Capability.MEMORY_READ],
            tools=[("mcp_echo", "echo", _SPAWN)],
        )
        run, _provider, audits = await _run(
            sqlite_db, graph, _echo_then_answer(), pools=pools, grants=_SPAWN
        )

        assert run.status is RunStatus.COMPLETED
        (tool_call,) = _tool_calls(audits)
        assert tool_call.error is None, "an unrelated agent capability must not deny the call"
        assert tool_call.outcome == {"value": "hello"}

        (recorded,) = pools[0].calls
        # The two sets are deliberately different here: the ceiling saw the
        # node's, the floor saw the agent's.
        assert recorded["declared_capabilities"] == set(_SPAWN)
        assert recorded["effective_capabilities"] == {*_SPAWN, Capability.MEMORY_READ}

    async def test_each_mcp_tool_node_meets_the_operators_ceiling_on_its_own(self, sqlite_db):
        """Two attachments on one agent, and only the second exceeds the grants.

        ``echo`` declares what the operator granted; ``add`` declares
        ``secret_access`` on top, which no grant covers. The agent holds both,
        so the runner's tool gate passes each of them and the pool is the gate
        that decides -- which is what makes this a statement about the ceiling
        rather than about the floor.

        The denial has to name the ``mcp_tool`` node, because that is the thing
        an author can change in response to it; naming the agent pointed the
        operator at widening a grant instead.
        """
        pools: list[_RecordingPool] = []
        graph = await _graph(
            agent_capabilities=[*_SPAWN, Capability.SECRET_ACCESS],
            tools=[
                ("mcp_echo", "echo", _SPAWN),
                ("mcp_add", "add", [*_SPAWN, Capability.SECRET_ACCESS]),
            ],
        )
        run, _provider, audits = await _run(
            sqlite_db,
            graph,
            [
                ProviderResponse(
                    content=None,
                    tool_calls=[
                        {"id": "call-1", "name": "echo", "args": {"text": "hello"}},
                        {"id": "call-2", "name": "add", "args": {"a": 1, "b": 2}},
                    ],
                ),
                ProviderResponse(content='{"echoed": "hello"}'),
            ],
            pools=pools,
            grants=_SPAWN,
        )

        assert run.status is RunStatus.COMPLETED
        echoed, added = _tool_calls(audits)

        assert echoed.tool_ref == "node://mcp_echo"
        assert echoed.error is None
        assert echoed.outcome == {"value": "hello"}

        assert added.tool_ref == "node://mcp_add"
        assert added.error is not None
        assert "mcp_tool node 'mcp_add'" in added.error, added.error
        assert "secret_access" in added.error, added.error
        # Refused ahead of the transport, so no effect happened and the record
        # must not imply one. The marker is a statement about dispatch, not a
        # decoration on every MCP-shaped failure.
        assert added.operation_support is None
        assert added.operation_residual_duplicate_risk is None

        # Both attachments were carried to the pool; the ceiling is per node,
        # not per agent.
        assert [call["tool_node_id"] for call in pools[0].calls] == ["mcp_echo", "mcp_add"]

    async def test_a_transport_failure_still_carries_the_at_least_once_marker(self, sqlite_db):
        """Finding 9, in the case that matters: the call may have landed.

        The failure is injected one layer *below* the pool, so the pool's own
        error typing and the runner's reading of it are both the real ones.
        Once ``call_tool`` has been entered the runtime cannot know whether the
        effect applied, and an unmarked record reads as though the operation
        boundary covered it.
        """
        # Taken before the patch: the pin is read through the same transport
        # this test is about to break.
        await _pin("echo")
        pools: list[_RecordingPool] = []
        graph = await _graph(
            agent_capabilities=_SPAWN,
            tools=[("mcp_echo", "echo", _SPAWN)],
        )

        async def broken_call_tool(self: Any, name: str, arguments: dict[str, Any]) -> Any:
            raise ConnectionResetError("connection reset mid-call")

        assert MCPClientManager.call_tool is not broken_call_tool
        with patch.object(MCPClientManager, "call_tool", broken_call_tool):
            run, _provider, audits = await _run(
                sqlite_db,
                graph,
                _echo_then_answer(answer="failed"),
                pools=pools,
            )

        assert run.status is RunStatus.COMPLETED
        (tool_call,) = _tool_calls(audits)
        assert tool_call.error is not None, "the failure must be audited"
        assert tool_call.operation_support == "at_least_once"
        assert tool_call.operation_residual_duplicate_risk is True

    async def test_an_agent_denied_the_capabilities_its_tool_declares_never_dispatches(
        self, sqlite_db
    ):
        """REGRESSION GUARD -- correct at HEAD, and until now never executed.

        The agent floor: the ``mcp_tool`` node declares ``process_spawn`` and
        ``external_api_call``, and the guard granted the agent neither, so the
        runner's tool gate refuses before anything is dispatched. That gate has
        resolved an ``MCPToolNode`` target's own capabilities since the node
        kind was introduced, so this test cannot fail against the pre-fix tree
        -- it is here because no test drove the property through the runtime at
        all, and because the negative half of the at-least-once marker is only
        meaningful next to the positive one above.
        """
        pools: list[_RecordingPool] = []
        graph = await _graph(
            agent_capabilities=[],
            tools=[("mcp_echo", "echo", _SPAWN)],
        )
        run, _provider, audits = await _run(
            sqlite_db, graph, _echo_then_answer(answer="denied"), pools=pools
        )

        assert run.status is RunStatus.COMPLETED
        (tool_call,) = _tool_calls(audits)
        assert tool_call.error is not None
        assert "capability denied for node 'agent'" in tool_call.error, tool_call.error
        assert "process_spawn" in tool_call.error, tool_call.error
        # Nothing reached the pool, so no process was spawned and no effect
        # could have happened -- which is why the record carries no marker.
        assert pools[0].calls == []
        assert tool_call.operation_support is None
        assert tool_call.operation_residual_duplicate_risk is None

    async def test_a_deployment_with_no_mcp_registry_fails_the_call_closed(self, sqlite_db):
        """REGRESSION GUARD -- correct at HEAD, and until now never executed.

        Without ``mcp_server_resolver`` the orchestrator owns no pool for the
        run, and the executor must refuse rather than fall through to some other
        dispatch path: an ``mcp_tool`` node with nothing to dispatch it is a
        governed attachment running ungoverned. The refusal is
        ``NodeDispatcherError``, which the runner feeds back to the model as a
        tool error, so the run survives and the record names the gap.

        This branch is unchanged by the current fixes, so it cannot fail against
        the pre-fix tree; the line trace that motivated this file recorded zero
        hits on it, which is the reason it is written.
        """
        pools: list[_RecordingPool] = []
        graph = await _graph(
            agent_capabilities=_SPAWN,
            tools=[("mcp_echo", "echo", _SPAWN)],
        )
        run, _provider, audits = await _run(
            sqlite_db,
            graph,
            _echo_then_answer(answer="unwired"),
            pools=pools,
            wire_resolver=False,
        )

        assert run.status is RunStatus.COMPLETED
        (tool_call,) = _tool_calls(audits)
        assert tool_call.error is not None
        assert "no MCP session pool is wired" in tool_call.error, tool_call.error
        assert "mcp_echo" in tool_call.error, tool_call.error
        # No pool was ever constructed for the run, so nothing was spawned.
        assert pools == []
        assert tool_call.operation_support is None
        assert tool_call.operation_residual_duplicate_risk is None

    async def test_a_teardown_failure_does_not_replace_the_runs_own_outcome(
        self, sqlite_db, caplog
    ) -> None:
        """The run's result survives a pool that will not close, and the failure is logged.

        Two properties, and the second is why this test is behavioural rather
        than a check that the teardown is written a particular way. The
        orchestrator must not let a stray "server would not stop" discard a
        result the graph already produced -- but for most of this branch's life
        that was spelled ``contextlib.suppress(Exception)``, which also
        discarded the *only* signal that a session had been stranded. A
        cross-task close failure raised on every parallel run and looked
        exactly like a clean teardown.

        So: the run still completes with its real answer, and the exception is
        on the record. A structural guard matching the token ``suppress``
        asserted a representation of the first property and was blind to the
        second; this asserts both by driving the orchestrator with a pool whose
        ``stop`` raises.
        """
        pools: list[_RecordingPool] = []
        graph = await _graph(agent_capabilities=_SPAWN, tools=[("mcp_echo", "echo", _SPAWN)])

        with caplog.at_level(logging.ERROR, logger="zeroth.runtime.orchestration.orchestrator"):
            run, _provider, audits = await _run(
                sqlite_db,
                graph,
                _echo_then_answer(),
                pools=pools,
                pool_class=_UnstoppablePool,
            )

        # The graph's own outcome is what the run reports, not the teardown's.
        assert run.status is RunStatus.COMPLETED
        (tool_call,) = _tool_calls(audits)
        assert tool_call.error is None
        assert tool_call.outcome == {"value": "hello"}

        # And the stranded session is not silent.
        assert any(
            "did not stop cleanly" in record.getMessage() for record in caplog.records
        ), [record.getMessage() for record in caplog.records]
