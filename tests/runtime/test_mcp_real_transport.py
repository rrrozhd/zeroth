"""End-to-end proofs against a real MCP server over real stdio.

Every other MCP test in this repository patches ``stdio_client`` and
``ClientSession``. That leaves the whole transport unproven: a mock will happily
confirm a handshake that never happens, a schema that never crossed a pipe, and
a subprocess that was never reaped. These tests spawn
``tests/runtime/mcp_fixtures/echo_server.py`` for real.

They are the only place the digest, the pool and the drift check are exercised
against a server that can actually disagree with them -- and the only place the
*process* claims can be checked at all. "Was the child reaped", "did a failed
handshake orphan one", "did four racing branches spawn four servers" are
questions about pids; a mock answers all three by construction and none of them
truthfully.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from zeroth.governance.policy.errors import CapabilityDeniedError
from zeroth.governance.policy.models import Capability
from zeroth.runtime.agents.mcp import (
    MCPClientManager,
    MCPSchemaDriftError,
    MCPServerConfig,
    RegisteredMCPServerConfig,
    tool_schema_hash,
)
from zeroth.runtime.agents.mcp_pool import MCPCeilingExceededError, MCPSessionPool

_SPAWN = {Capability.PROCESS_SPAWN, Capability.EXTERNAL_API_CALL}
_FIXTURE = ["-m", "tests.runtime.mcp_fixtures.echo_server"]


def _config(*, drift: bool = False, grants: list[Capability] | None = None) -> RegisteredMCPServerConfig:
    return RegisteredMCPServerConfig(
        name="echo",
        command=sys.executable,
        args=list(_FIXTURE),
        env={"ZEROTH_FIXTURE_DRIFT": "1"} if drift else None,
        grants=list(_SPAWN) if grants is None else grants,
    )


def _resolver(*, drift: bool = False, grants: list[Capability] | None = None):
    async def resolve(server_ref: str) -> RegisteredMCPServerConfig | None:
        return _config(drift=drift, grants=grants) if server_ref == "echo" else None

    return resolve


def _pid_recording_args(pid_dir: Path, body: str) -> list[str]:
    """Run *body* in a child that first writes its own pid into *pid_dir*.

    The fixture module is shared with the other tests and is not this module's
    to edit, so the pid is captured by wrapping it rather than by changing it.
    Every child that reaches ``exec`` leaves a file behind, which is what makes
    "the process was reaped" and "the process was orphaned" answerable at all --
    ``pool._managers`` answers neither, because it is cleared before anything is
    stopped.
    """
    return [
        "-c",
        (
            "import os, pathlib\n"
            f"pathlib.Path({str(pid_dir)!r}).joinpath(str(os.getpid())).write_text('x')\n"
            f"{body}\n"
        ),
    ]


_RUN_FIXTURE = "import runpy; runpy.run_module('tests.runtime.mcp_fixtures.echo_server', run_name='__main__')"
#: A child that survives a handshake it cannot complete. Closing stdout makes
#: the client's read stream hit EOF immediately -- so the failure is fast -- while
#: the process itself stays alive, which is exactly the orphan a dropped-but-
#: unclosed manager leaves behind.
_SURVIVES_A_FAILED_HANDSHAKE = "import os, time; os.close(1); time.sleep(300)"


def _live_pids(pid_dir: Path) -> list[int]:
    """Which of the children that started are still running."""
    alive = []
    for entry in pid_dir.iterdir():
        pid = int(entry.name)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        alive.append(pid)
    return alive


@pytest.fixture
def pid_dir(tmp_path: Path):
    """A directory of pid files, with every survivor killed on the way out."""
    directory = tmp_path / "pids"
    directory.mkdir()
    yield directory
    for pid in _live_pids(directory):
        os.kill(pid, 9)


@pytest.fixture
def spawn_counter():
    """Count real spawns without replacing one.

    ``start`` is wrapped, not stubbed: the handshake, the tool listing and the
    child process are all still real, so the count is of processes that actually
    exist rather than of calls to a stand-in.

    Handed to the test as a context manager rather than installed for the whole
    test, because ``_pin_for`` spawns a server of its own to read the pin. A
    fixture-wide patch counted those too and made every one of these assertions
    a puzzle about the harness instead of a claim about the pool. Take the pins
    first, then open the counter around the part under test.
    """
    import contextlib

    @contextlib.contextmanager
    def counting():
        real_start = MCPClientManager.start
        starts: list[str] = []

        async def counting_start(self):
            starts.append(self._configs[0].name)
            return await real_start(self)

        with patch.object(MCPClientManager, "start", counting_start):
            yield starts

    return counting


@pytest.fixture
def stop_spy():
    """Record real ``stop`` calls without replacing one.

    The pid alone cannot carry this claim. A manager nobody closed still loses
    its last reference when the owning task ends, and garbage collection then
    shuts the transport's pipes -- so the child dies either way and a
    pid-only assertion passes against a teardown that does nothing. What must be
    true is that the pool closed the session *deliberately*, and only the call
    itself says that.
    """
    import contextlib

    @contextlib.contextmanager
    def spying():
        real_stop = MCPClientManager.stop
        stops: list[str] = []

        async def recording_stop(self):
            stops.append(self._configs[0].name)
            return await real_stop(self)

        with patch.object(MCPClientManager, "stop", recording_stop):
            yield stops

    return spying


async def _pin_for(tool: str, *, drift: bool = False) -> str:
    """The digest an import would freeze, taken from the live server."""
    manager = MCPClientManager([_config(drift=drift)])
    try:
        manifests = await manager.start()
    finally:
        await manager.stop()
    manifest = next(m for m in manifests if m.alias == tool)
    return tool_schema_hash(manifest.alias, manifest.description, manifest.parameters_schema)


async def _call(
    pool: MCPSessionPool,
    *,
    agent_node_id="agent_a",
    tool_node_id="mcp_echo",
    declared=None,
    caps=None,
    pin=None,
    tool="echo",
    arguments=None,
):
    return await pool.call(
        server_ref="echo",
        tool_name=tool,
        arguments={"text": "hi"} if arguments is None else arguments,
        agent_node_id=agent_node_id,
        tool_node_id=tool_node_id,
        declared_capabilities=_SPAWN if declared is None else declared,
        effective_capabilities=_SPAWN if caps is None else caps,
        pinned_hash=pin if pin is not None else await _pin_for(tool),
    )


@pytest.mark.asyncio
async def test_a_real_server_is_spawned_and_answers() -> None:
    """The transport itself: spawn, handshake, list, call, shut down."""
    pool = MCPSessionPool(_resolver())
    try:
        assert await _call(pool) == "hi"
        assert await _call(pool, tool="add", arguments={"a": 2, "b": 40}) == "42"
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_two_nodes_really_do_share_one_process(spawn_counter) -> None:
    """Pooling, proved against real processes rather than a stand-in."""
    pin = await _pin_for("echo")
    pool = MCPSessionPool(_resolver())
    with spawn_counter() as starts:
        try:
            await _call(pool, agent_node_id="agent_a", tool_node_id="mcp_a", pin=pin)
            await _call(pool, agent_node_id="agent_b", tool_node_id="mcp_b", pin=pin)
            assert starts == ["echo"]
        finally:
            await pool.stop()


@pytest.mark.asyncio
async def test_racing_branches_really_do_share_one_process(pid_dir, spawn_counter) -> None:
    """The concurrency the pool exists inside, against real processes.

    ``RuntimeParallelExecutor`` starts branches with ``create_task``/``gather``,
    every branch of a run carries the parent's ``run_id``, and the dispatcher
    selects the pool by ``run_id`` -- so all branches of one run share this pool
    and their first calls to a server arrive together. Without a lock each one
    missed the membership check and spawned its own server; the last assignment
    won and the rest became unreachable, so ``stop`` could not close them.

    The observable has to be the pid count. ``len(pool._managers)`` is ``1``
    either way -- last write wins -- so it passes just as happily over four
    leaked processes as over one shared session.
    """
    pin = await _pin_for("echo")

    async def resolve(server_ref: str) -> RegisteredMCPServerConfig | None:
        return RegisteredMCPServerConfig(
            name="echo",
            command=sys.executable,
            args=_pid_recording_args(pid_dir, _RUN_FIXTURE),
            grants=list(_SPAWN),
        )

    pool = MCPSessionPool(resolve)
    with spawn_counter() as starts:
        try:
            results = await asyncio.gather(
                *(_call(pool, agent_node_id=f"branch_{i}", pin=pin) for i in range(4))
            )
        finally:
            await pool.stop()

    assert results == ["hi"] * 4
    assert starts == ["echo"], "each racing branch spawned its own server"
    assert len(list(pid_dir.iterdir())) == 1, "four branches left more than one child behind"
    for _ in range(50):
        if not _live_pids(pid_dir):
            break
        await asyncio.sleep(0.1)
    assert _live_pids(pid_dir) == [], "the shared child outlived the run"


@pytest.mark.asyncio
async def test_a_denied_node_leaves_no_process_behind() -> None:
    """The gate has to beat the spawn, not merely precede the call."""
    pool = MCPSessionPool(_resolver())
    try:
        with pytest.raises(CapabilityDeniedError):
            await _call(pool, caps={Capability.PROCESS_SPAWN}, pin="unused")
        assert pool._managers == {}
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_a_node_above_the_operators_ceiling_never_spawns() -> None:
    pool = MCPSessionPool(_resolver(grants=[Capability.PROCESS_SPAWN]))
    try:
        with pytest.raises(MCPCeilingExceededError):
            await _call(pool, pin="unused")
        assert pool._managers == {}
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_a_server_that_really_changed_is_refused() -> None:
    """Real drift, not a hand-edited hash.

    The pin is taken from the server as it was at import; the run then faces a
    server whose ``echo`` description has genuinely changed. This is the case
    the whole pinning design exists for, and it is unreachable with a mock that
    returns whatever the test told it to.
    """
    pinned_at_import = await _pin_for("echo")
    drifted = await _pin_for("echo", drift=True)
    assert pinned_at_import != drifted, "fixture did not actually drift"

    pool = MCPSessionPool(_resolver(drift=True))
    try:
        with pytest.raises(MCPSchemaDriftError) as excinfo:
            await _call(pool, pin=pinned_at_import)
    finally:
        await pool.stop()
    assert excinfo.value.tool_name == "echo"
    assert excinfo.value.pinned == pinned_at_import


@pytest.mark.asyncio
async def test_an_unchanged_server_never_reads_as_drift() -> None:
    """The other half: canonicalisation must survive a real round trip.

    If the digest depended on key ordering or spacing as the server happened to
    serialise it, this would fail intermittently and the fail-closed check would
    fire on an innocent restart.
    """
    first = await _pin_for("echo")
    second = await _pin_for("echo")
    assert first == second

    pool = MCPSessionPool(_resolver())
    try:
        assert await _call(pool, pin=first) == "hi"
    finally:
        await pool.stop()


@pytest.mark.asyncio
async def test_stop_reaps_the_child_process(pid_dir, stop_spy) -> None:
    """A pool that forgets to reap leaks a process per run.

    Neither half of this is what the old test asserted. It checked
    ``_managers == {}`` after ``stop`` -- but ``stop`` clears that dict *before*
    it closes anything, so the assertion held whether every session was closed
    or none of them were; no-op the teardown and the test stayed green over a
    live child.

    Both halves are needed to replace it. The pid says nothing survived the run;
    the spy says the pool is what ended it, rather than the garbage collector
    happening to drop the last reference to an unclosed transport.
    """
    pin = await _pin_for("echo")

    async def resolve(server_ref: str) -> RegisteredMCPServerConfig | None:
        return RegisteredMCPServerConfig(
            name="echo",
            command=sys.executable,
            args=_pid_recording_args(pid_dir, _RUN_FIXTURE),
            grants=list(_SPAWN),
        )

    pool = MCPSessionPool(resolve)
    with stop_spy() as stops:
        await _call(pool, pin=pin)
        started = [int(entry.name) for entry in pid_dir.iterdir()]
        assert len(started) == 1
        assert _live_pids(pid_dir) == started, "the fixture never actually ran"

        await pool.stop()
        assert stops == ["echo"], "stop() returned without closing the session it opened"

    # The child exits once its stdin closes; give the OS a moment rather than
    # racing it, but not so long that a leak looks like a slow shutdown.
    for _ in range(50):
        if not _live_pids(pid_dir):
            break
        await asyncio.sleep(0.1)
    assert _live_pids(pid_dir) == [], "stop() left the MCP server process running"


class TestAFailedHandshakeCleansUpAfterItself:
    """The pool must survive a server that spawns and then cannot be talked to.

    ``MCPClientManager.start`` enters a ``stdio_client`` transport -- which is
    where the child is forked -- *before* the ``initialize`` it can fail on. A
    manager that only became reachable after a successful ``start`` therefore
    left, per failure: a child nothing could reap, and an anyio cancel scope
    entered and never exited inside the *calling* task. In production that
    caller is the run's own drive loop, so a slow or crash-on-boot MCP server
    stopped being an MCP problem and became a graph-execution problem.
    """

    @staticmethod
    def _resolver_for(args: list[str]):
        async def resolve(server_ref: str) -> RegisteredMCPServerConfig | None:
            return RegisteredMCPServerConfig(
                name="broken", command=sys.executable, args=args, grants=list(_SPAWN)
            )

        return resolve

    @staticmethod
    async def _attempt(pool: MCPSessionPool) -> BaseException | None:
        try:
            await pool.call(
                server_ref="broken",
                tool_name="echo",
                arguments={},
                agent_node_id="agent_a",
                tool_node_id="mcp_echo",
                declared_capabilities=_SPAWN,
                effective_capabilities=_SPAWN,
                pinned_hash="deadbeef" * 8,
            )
        except BaseException as exc:  # noqa: BLE001 - the point is to inspect it
            return exc
        return None

    @pytest.mark.asyncio
    async def test_a_child_that_outlives_its_handshake_is_still_reaped(self, pid_dir) -> None:
        """The orphan, observed as a pid rather than inferred from a dict."""
        pool = MCPSessionPool(
            self._resolver_for(_pid_recording_args(pid_dir, _SURVIVES_A_FAILED_HANDSHAKE))
        )
        failure = await self._attempt(pool)
        assert failure is not None, "the handshake was expected to fail"

        assert len(list(pid_dir.iterdir())) == 1, "the child never started"
        for _ in range(50):
            if not _live_pids(pid_dir):
                break
            await asyncio.sleep(0.1)
        assert _live_pids(pid_dir) == [], (
            "a failed handshake orphaned its child: the manager was dropped before it "
            "was ever registered, so nothing could close what start() had entered"
        )
        assert pool._managers == {}, "a corpse was left where the next call would reuse it"

    @pytest.mark.asyncio
    async def test_repeated_failures_do_not_corrupt_the_callers_cancel_scope(self) -> None:
        """The symptom that turns one bad server into a broken run.

        An un-exited anyio cancel scope stays on the calling task's scope stack.
        From the second failure onward the task's own cancellation state is
        wrong: an ordinary ``asyncio.sleep`` raised ``CancelledError`` at 0.0s.
        Three failures then a plain sleep is the whole reproduction.
        """
        pool = MCPSessionPool(self._resolver_for(["-c", "raise SystemExit(1)"]))
        for _ in range(3):
            failure = await self._attempt(pool)
            assert failure is not None
            assert not isinstance(failure, asyncio.CancelledError), (
                "a failed handshake cancelled the caller instead of raising its own error"
            )

        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            await asyncio.sleep(0.3)
        except asyncio.CancelledError:
            pytest.fail("a failed MCP handshake corrupted the calling task's cancel scope")
        assert loop.time() - started >= 0.25, "the sleep returned early -- the scope is poisoned"
