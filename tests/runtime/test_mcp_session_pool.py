"""Proofs for the run-scoped MCP session pool.

Four properties carry the design: sessions are shared within a run, the
capability gate fires before a process exists AND once per attachment (not once
per spawn), a drifted pin refuses the call, and the two subjects the gate
compares -- the ``mcp_tool`` node against the operator's grants, the agent
against the capability floor -- stay apart.

The transport is mocked here on purpose; the process-lifecycle properties a mock
cannot observe live in ``test_mcp_real_transport.py``.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from zeroth.governance.policy.errors import CapabilityDeniedError
from zeroth.governance.policy.models import Capability
from zeroth.runtime.agents.mcp import (
    MCPSchemaDriftError,
    MCPServerConfig,
    RegisteredMCPServerConfig,
    tool_schema_hash,
)
from zeroth.runtime.agents.mcp_pool import (
    MCPCeilingExceededError,
    MCPSessionPool,
    MCPToolDispatchError,
    UnknownMCPServerError,
)
from zeroth.runtime.agents.tools import ToolAttachmentManifest

_ALLOWED = {Capability.PROCESS_SPAWN, Capability.EXTERNAL_API_CALL}
_SCHEMA: dict[str, Any] = {"type": "object", "properties": {"path": {"type": "string"}}}
_PINNED = tool_schema_hash("read_file", "Read a file", _SCHEMA)


def _manifest(name: str = "read_file", description: str = "Read a file") -> ToolAttachmentManifest:
    return ToolAttachmentManifest(
        alias=name,
        executable_unit_ref=f"mcp://filesystem/{name}",
        description=description,
        parameters_schema=_SCHEMA,
    )


async def _resolver(server_ref: str) -> RegisteredMCPServerConfig | None:
    if server_ref != "filesystem":
        return None
    return RegisteredMCPServerConfig(
        name="filesystem", command="echo", args=[], grants=sorted(_ALLOWED, key=lambda c: c.value)
    )


@pytest.fixture
def spawned():
    """Track spawns so sharing can be asserted, not assumed."""
    calls: list[str] = []

    async def fake_start(self):
        calls.append(self._configs[0].name)
        # A real spawn is I/O and therefore a suspension point. Without one here
        # a gathered call never actually interleaves, and the concurrency test
        # below would pass against a pool with no locking at all.
        await asyncio.sleep(0)
        return [_manifest()]

    with (
        patch("zeroth.runtime.agents.mcp.MCPClientManager.start", new=fake_start),
        patch(
            "zeroth.runtime.agents.mcp.MCPClientManager.call_tool",
            new=AsyncMock(return_value="ok"),
        ),
        patch("zeroth.runtime.agents.mcp.MCPClientManager.stop", new=AsyncMock()),
    ):
        yield calls


@pytest.fixture
async def make_pool():
    """Build pools that get stopped, because a run always ends.

    A test that walks away from a pool leaves its session owner parked, and the
    loop's teardown then cancels it -- so the test quietly exercises the
    cancellation path instead of the ordinary close. Stopping here keeps each
    test's subject the thing it says it is.
    """
    import contextlib

    pools: list[MCPSessionPool] = []

    def build(resolver) -> MCPSessionPool:
        pool = MCPSessionPool(resolver)
        pools.append(pool)
        return pool

    yield build

    for pool in pools:
        with contextlib.suppress(Exception):
            await pool.stop()


async def _call(
    pool: MCPSessionPool,
    *,
    agent_node_id: str = "agent_a",
    tool_node_id: str = "mcp_read_file",
    declared=_ALLOWED,
    caps=_ALLOWED,
    pinned=_PINNED,
):
    return await pool.call(
        server_ref="filesystem",
        tool_name="read_file",
        arguments={"path": "/tmp/x"},
        agent_node_id=agent_node_id,
        tool_node_id=tool_node_id,
        declared_capabilities=declared,
        effective_capabilities=caps,
        pinned_hash=pinned,
    )


@pytest.mark.asyncio
async def test_two_nodes_in_one_run_share_a_single_process(spawned, make_pool) -> None:
    """The reason ownership moved off the agent runner."""
    pool = make_pool(_resolver)
    await _call(pool, agent_node_id="agent_a", tool_node_id="mcp_a")
    await _call(pool, agent_node_id="agent_b", tool_node_id="mcp_b")
    assert spawned == ["filesystem"]


@pytest.mark.asyncio
async def test_concurrent_first_calls_still_spawn_one_process(spawned, make_pool) -> None:
    """The ordering that actually happens under fan-out.

    ``RuntimeParallelExecutor`` starts branches with ``create_task``/``gather``
    and every branch of a run shares this pool, so the first calls to a server
    arrive *together*. An unlocked check-then-spawn had them all miss the
    membership test: each started its own process, the last assignment won, and
    the losers were unreachable by ``stop`` for the rest of the run.

    Awaiting the calls one after another -- as the sharing test above does --
    is the one ordering that cannot show this.
    """
    pool = make_pool(_resolver)
    await asyncio.gather(*(_call(pool) for _ in range(4)))
    assert spawned == ["filesystem"]
    assert list(pool._managers) == ["filesystem"]


@pytest.mark.asyncio
async def test_nothing_spawns_until_a_tool_is_actually_called(spawned, make_pool) -> None:
    """A branch the run never takes must not cost a process.

    Both halves are the claim: constructing the pool spawns nothing, and the
    *first call* is what spawns. Asserting only the first half passes against a
    pool that never spawns at all, which proves laziness by proving uselessness.
    """
    pool = make_pool(_resolver)
    assert spawned == []
    await _call(pool)
    assert spawned == ["filesystem"]


@pytest.mark.asyncio
async def test_a_denied_node_never_reaches_a_spawn(spawned, make_pool) -> None:
    """Spawning is a side effect; denying after it would be too late."""
    pool = make_pool(_resolver)
    with pytest.raises(CapabilityDeniedError):
        await _call(pool, caps={Capability.PROCESS_SPAWN})
    assert spawned == []


@pytest.mark.asyncio
async def test_a_second_agent_cannot_ride_in_on_the_first_agents_grant(spawned, make_pool) -> None:
    """The property that forced a per-call gate rather than a per-spawn gate.

    Both agents bind the *same* ``mcp_tool`` node, which is the arrangement that
    catches this. Give them different tool nodes and a gate that remembers only
    which tool node it cleared still denies agent_b -- for the wrong reason --
    so the hole ships green. Once agent_a has started the session, a gate that
    clears an attachment once waves agent_b through to a live process it was
    never entitled to.
    """
    pool = make_pool(_resolver)
    await _call(pool, agent_node_id="agent_a", tool_node_id="mcp_shared")
    with pytest.raises(CapabilityDeniedError):
        await _call(pool, agent_node_id="agent_b", tool_node_id="mcp_shared", caps=set())
    assert spawned == ["filesystem"]


@pytest.mark.asyncio
async def test_each_mcp_tool_node_on_one_agent_is_checked_separately(spawned, make_pool) -> None:
    """A gate that clears an agent once checks its first attachment and no other.

    One agent may bind several ``mcp_tool`` nodes against one server, and each
    carries its own declaration to measure against the operator's grants.
    Remembering that the *agent* had cleared let the second node inherit the
    first node's clearance and reach the server with a declaration nobody ever
    compared to anything.
    """
    pool = make_pool(_resolver)
    await _call(pool, agent_node_id="agent_a", tool_node_id="mcp_first")
    with pytest.raises(MCPCeilingExceededError) as excinfo:
        await _call(
            pool,
            agent_node_id="agent_a",
            tool_node_id="mcp_second",
            declared=_ALLOWED | {Capability.FILESYSTEM_WRITE},
        )
    assert excinfo.value.tool_node_id == "mcp_second"


@pytest.mark.asyncio
async def test_advisory_mode_does_not_gate_the_agent(spawned, make_pool) -> None:
    """``None`` means enforcement is unwired, matching the runner's convention."""
    pool = make_pool(_resolver)
    assert await _call(pool, caps=None) == "ok"


@pytest.mark.asyncio
async def test_the_ceiling_holds_even_in_advisory_mode(spawned, make_pool) -> None:
    """Advisory mode is a statement about the *agent's* capabilities, nothing more.

    ``caps(M)`` is static graph data and the server's grants are operator-owned,
    so neither depends on capability enforcement running for this run. Skipping
    the ceiling alongside the floor made the operator's assertion about their own
    server contingent on a policy switch it has nothing to do with -- and every
    deployment with no policy guard wired reached MCP servers ungated.
    """
    pool = make_pool(_resolver)
    with pytest.raises(MCPCeilingExceededError):
        await _call(pool, caps=None, declared=_ALLOWED | {Capability.FILESYSTEM_WRITE})
    assert spawned == []


@pytest.mark.asyncio
async def test_an_agents_unrelated_capabilities_are_not_measured_against_the_grants(
    spawned,
    make_pool,
) -> None:
    """The ceiling's subject is the ``mcp_tool`` node, never the agent.

    An agent holds capabilities for every tool it can call. Measuring *that* set
    against one server's grants demanded the operator grant their MCP server
    filesystem access before an agent that also writes files could call it --
    and the denial's own advice ("widen the server's grants") converges every
    server on the union of everything any agent holds, which is the control
    dissolving itself.
    """
    pool = make_pool(_resolver)
    result = await _call(
        pool,
        declared=_ALLOWED,
        caps=_ALLOWED | {Capability.FILESYSTEM_WRITE, Capability.NETWORK_WRITE},
    )
    assert result == "ok"


@pytest.mark.asyncio
async def test_a_drifted_tool_is_refused(spawned, make_pool) -> None:
    """Without this the pin is a stale copy the server may change at will."""
    pool = make_pool(_resolver)
    with pytest.raises(MCPSchemaDriftError) as excinfo:
        await _call(pool, pinned="deadbeef" * 8)
    assert excinfo.value.tool_name == "read_file"


@pytest.mark.asyncio
async def test_a_tool_the_server_no_longer_offers_is_drift_not_a_key_error(spawned, make_pool) -> None:
    pool = make_pool(_resolver)
    with pytest.raises(MCPSchemaDriftError):
        await pool.call(
            server_ref="filesystem",
            tool_name="deleted_tool",
            arguments={},
            agent_node_id="agent_a",
            tool_node_id="mcp_deleted",
            declared_capabilities=_ALLOWED,
            effective_capabilities=_ALLOWED,
            pinned_hash=_PINNED,
        )


@pytest.mark.asyncio
async def test_an_unregistered_server_is_named_clearly(spawned, make_pool) -> None:
    pool = make_pool(_resolver)
    with pytest.raises(UnknownMCPServerError):
        await pool.call(
            server_ref="never-registered",
            tool_name="read_file",
            arguments={},
            agent_node_id="agent_a",
            tool_node_id="mcp_read_file",
            declared_capabilities=_ALLOWED,
            effective_capabilities=_ALLOWED,
            pinned_hash=_PINNED,
        )


class TestOnlyADispatchedCallSaysSoByType:
    """``MCPToolDispatchError`` is a statement about how far the call got.

    An MCP call is at-least-once with no replay suppression, so the audit record
    has to distinguish "never invoked" from "may already have run". Deriving
    that from *which* error came back -- a negative test over failure types --
    is what let the distinction go missing: every failure mode added later
    defaults to the wrong side. A positive signal raised at exactly one place
    cannot drift that way.
    """

    @pytest.mark.asyncio
    async def test_a_transport_failure_is_a_dispatch_error(self, spawned, make_pool) -> None:
        pool = make_pool(_resolver)
        boom = ConnectionResetError("server hung up mid-call")
        with patch(
            "zeroth.runtime.agents.mcp.MCPClientManager.call_tool",
            new=AsyncMock(side_effect=boom),
        ):
            with pytest.raises(MCPToolDispatchError) as excinfo:
                await _call(pool)
        assert excinfo.value.tool_name == "read_file"
        assert excinfo.value.server_ref == "filesystem"
        assert excinfo.value.__cause__ is boom

    @pytest.mark.asyncio
    async def test_everything_refused_before_dispatch_keeps_its_own_type(self, spawned, make_pool) -> None:
        """None of these reached the server, so none may claim it might have run."""
        pool = make_pool(_resolver)

        with pytest.raises(CapabilityDeniedError):
            await _call(pool, caps=set())
        with pytest.raises(MCPCeilingExceededError):
            await _call(pool, declared=_ALLOWED | {Capability.FILESYSTEM_WRITE})
        with pytest.raises(MCPSchemaDriftError):
            await _call(pool, pinned="deadbeef" * 8)
        with pytest.raises(UnknownMCPServerError):
            await pool.call(
                server_ref="never-registered",
                tool_name="read_file",
                arguments={},
                agent_node_id="agent_a",
                tool_node_id="mcp_read_file",
                declared_capabilities=_ALLOWED,
                effective_capabilities=_ALLOWED,
                pinned_hash=_PINNED,
            )

    @pytest.mark.asyncio
    async def test_a_cancelled_call_stays_cancelled(self, spawned, make_pool) -> None:
        """Wrapping ``CancelledError`` in a ``RuntimeError`` swallows the cancellation."""
        pool = make_pool(_resolver)
        with patch(
            "zeroth.runtime.agents.mcp.MCPClientManager.call_tool",
            new=AsyncMock(side_effect=asyncio.CancelledError()),
        ):
            with pytest.raises(asyncio.CancelledError):
                await _call(pool)


@pytest.mark.asyncio
async def test_a_failed_spawn_is_closed_and_leaves_no_half_built_session(make_pool) -> None:
    """The next call must retry the spawn, not treat a corpse as live.

    Both halves matter and only one of them is visible in ``_managers``. A
    manager dropped without being closed still holds whatever ``start`` entered
    before it raised, and nothing can reach it afterwards -- so the stop is
    asserted directly rather than inferred from an empty dict.
    """
    stops: list[str] = []

    async def exploding_start(self):
        raise RuntimeError("server died on handshake")

    async def recording_stop(self):
        stops.append(self._configs[0].name)

    pool = make_pool(_resolver)
    with (
        patch("zeroth.runtime.agents.mcp.MCPClientManager.start", new=exploding_start),
        patch("zeroth.runtime.agents.mcp.MCPClientManager.stop", new=recording_stop),
    ):
        with pytest.raises(RuntimeError):
            await _call(pool)
        assert stops == ["filesystem"], "a failed spawn was dropped without being closed"
        assert pool._managers == {}


@pytest.mark.asyncio
async def test_a_stop_that_fails_does_not_replace_the_spawn_failure(make_pool) -> None:
    """The handshake error is why the call failed; the cleanup error is noise."""

    async def exploding_start(self):
        raise RuntimeError("server died on handshake")

    async def exploding_stop(self):
        raise OSError("could not reap")

    pool = make_pool(_resolver)
    with (
        patch("zeroth.runtime.agents.mcp.MCPClientManager.start", new=exploding_start),
        patch("zeroth.runtime.agents.mcp.MCPClientManager.stop", new=exploding_stop),
    ):
        with pytest.raises(RuntimeError, match="died on handshake"):
            await _call(pool)


@pytest.mark.asyncio
async def test_stop_closes_every_session_even_if_one_fails(make_pool) -> None:
    """One bad shutdown must not strand the other processes."""
    stopped: list[str] = []

    async def fake_start(self):
        return [_manifest()]

    async def flaky_stop(self):
        name = self._configs[0].name
        stopped.append(name)
        if name == "filesystem":
            raise RuntimeError("stop failed")

    async def two_servers(server_ref: str) -> RegisteredMCPServerConfig | None:
        return RegisteredMCPServerConfig(
            name=server_ref,
            command="echo",
            args=[],
            grants=sorted(_ALLOWED, key=lambda c: c.value),
        )

    pool = make_pool(two_servers)
    with (
        patch("zeroth.runtime.agents.mcp.MCPClientManager.start", new=fake_start),
        patch(
            "zeroth.runtime.agents.mcp.MCPClientManager.call_tool",
            new=AsyncMock(return_value="ok"),
        ),
        patch("zeroth.runtime.agents.mcp.MCPClientManager.stop", new=flaky_stop),
    ):
        await _call(pool)
        await pool.call(
            server_ref="git",
            tool_name="read_file",
            arguments={},
            agent_node_id="agent_a",
            tool_node_id="mcp_git",
            declared_capabilities=_ALLOWED,
            effective_capabilities=_ALLOWED,
            pinned_hash=_PINNED,
        )
        with pytest.raises(RuntimeError):
            await pool.stop()

    assert sorted(stopped) == ["filesystem", "git"]


@pytest.mark.asyncio
async def test_a_node_above_the_servers_ceiling_is_denied_at_run_time(spawned, make_pool) -> None:
    """The publish check is not enough on its own.

    A published graph version is immutable, so a node validated against
    yesterday's grants would otherwise keep a capability the operator has since
    withdrawn. This is also the only check standing if a deployment ever builds
    its validator without the grants resolver.
    """

    async def narrow(server_ref: str) -> RegisteredMCPServerConfig | None:
        return RegisteredMCPServerConfig(
            name="filesystem",
            command="echo",
            args=[],
            grants=[Capability.PROCESS_SPAWN, Capability.EXTERNAL_API_CALL],
        )

    pool = make_pool(narrow)
    with pytest.raises(MCPCeilingExceededError) as excinfo:
        await _call(pool, declared=_ALLOWED | {Capability.FILESYSTEM_WRITE})
    assert "filesystem_write" in excinfo.value.excess
    assert spawned == []


class TestTheAtLeastOnceMarkerSurvivedTheMove:
    """The marker must key on the target node, not on a ref prefix.

    ``mcp_at_least_once`` originally fired on ``executable_unit_ref`` starting
    with ``mcp://``. Tool edges mint ``node://<id>``, so once MCP tools became
    graph nodes the marker stopped firing and an MCP call's audit record became
    indistinguishable from one that really does carry an operation receipt --
    the exact distinction this node kind exists to keep visible.
    """

    def test_both_sides_share_one_marker_key(self) -> None:
        """A key each side spells for itself is a key that can silently diverge."""
        from zeroth.runtime.agents import factory, runner
        from zeroth.runtime.agents.factory_markers import MCP_AT_LEAST_ONCE

        assert factory.MCP_AT_LEAST_ONCE is MCP_AT_LEAST_ONCE
        assert runner.MCP_AT_LEAST_ONCE is MCP_AT_LEAST_ONCE

    @pytest.mark.asyncio
    async def test_the_factory_stamps_only_mcp_targets(self) -> None:
        """A stamp on every tool would mark guarded calls as unguaranteed.

        Asserted on the manifests the factory actually produces. The previous
        version of this test searched ``inspect.getsource`` for the two names it
        expected to see, which is satisfied just as well by a condition that
        stamps the wrong branch -- inverting the ``isinstance`` check leaves both
        substrings exactly where they were.
        """
        from zeroth.runtime.agents.factory import build_agent_runners
        from zeroth.runtime.agents.factory_markers import MCP_AT_LEAST_ONCE

        graph = _graph_with_one_mcp_and_one_unit_tool()
        runners = await build_agent_runners(
            graph, _StubContractRegistry(), provider=object()
        )
        manifests = {
            attachment.alias: attachment
            for attachment in runners["agent"].config.tool_attachments
        }

        assert manifests["ask_mcp"].metadata.get(MCP_AT_LEAST_ONCE) is True
        assert MCP_AT_LEAST_ONCE not in manifests["run_unit"].metadata


class _StubContractRegistry:
    """Resolves any contract ref to one throwaway model.

    The factory needs contracts resolved before it will build a runner; which
    contracts they are is irrelevant to what this test asserts.
    """

    async def resolve_model_type(self, reference: Any) -> type:
        from pydantic import BaseModel

        return type("Payload", (BaseModel,), {})


def _graph_with_one_mcp_and_one_unit_tool():
    """One agent, two tool edges: one to an ``mcp_tool`` node, one to a unit."""
    from zeroth.contracts.graph.models import (
        AgentNode,
        AgentNodeData,
        AgentToolBinding,
        ExecutableUnitNode,
        ExecutableUnitNodeData,
        Graph,
        MCPToolNode,
        MCPToolNodeData,
    )

    return Graph(
        graph_id="g",
        name="g",
        nodes=[
            AgentNode(
                node_id="agent",
                graph_version_ref="g@1",
                input_contract_ref="in",
                output_contract_ref="out",
                capability_bindings=["process_spawn", "external_api_call"],
                agent=AgentNodeData(
                    model_provider="stub",
                    instruction="do the thing",
                    tool_bindings=[
                        AgentToolBinding(
                            name="ask_mcp",
                            description="call the MCP tool",
                            target_node_id="mcp_node",
                        ),
                        AgentToolBinding(
                            name="run_unit",
                            description="call the unit",
                            target_node_id="unit_node",
                        ),
                    ],
                ),
            ),
            MCPToolNode(
                node_id="mcp_node",
                graph_version_ref="g@1",
                input_contract_ref="in",
                output_contract_ref="out",
                capability_bindings=["process_spawn", "external_api_call"],
                mcp_tool=MCPToolNodeData(
                    server_ref="filesystem",
                    tool_name="read_file",
                    description="Read a file",
                    input_schema=_SCHEMA,
                    schema_hash=_PINNED,
                ),
            ),
            ExecutableUnitNode(
                node_id="unit_node",
                graph_version_ref="g@1",
                input_contract_ref="in",
                output_contract_ref="out",
                executable_unit=ExecutableUnitNodeData(
                    manifest_ref="unit://noop@1", execution_mode="native"
                ),
            ),
        ],
        edges=[],
    )
