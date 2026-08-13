"""MCP startup is deadlined and leak-free (ZER-48 / A06-9, A06-10).

Two defects lived in the same few lines.

* ``_start_mcp_servers`` was awaited on the line *before* the ``try`` whose
  ``finally`` stops the servers.  ``MCPClientManager.start`` enters one
  ``stdio_client`` and one ``ClientSession`` per configured server and raises out
  of the loop on the first failure, so a run whose second server failed left the
  first one entered, with nothing on any path able to close it.
* No await in the module carried a deadline, and startup runs before the agent's
  own ``timeout_seconds`` covers anything, so a server that connected and then
  never answered hung the run with no bound anywhere.
"""

from __future__ import annotations

import ast
import asyncio
import inspect

import pytest

from zeroth.runtime.agents.mcp import MCPClientManager, MCPServerConfig, MCPTimeoutError


class _HangingSession:
    """A session that connects and then never answers."""

    async def initialize(self) -> None:
        await asyncio.Event().wait()

    async def list_tools(self):  # noqa: ANN202  # pragma: no cover - never reached
        await asyncio.Event().wait()

    async def call_tool(self, name: str, arguments: dict) -> object:
        del name, arguments
        await asyncio.Event().wait()


class _SilentToolSession(_HangingSession):
    """Handshake succeeds; only the tool call hangs."""

    async def initialize(self) -> None:
        return None

    async def list_tools(self):  # noqa: ANN202
        return type("Response", (), {"tools": []})()


def _manager(session: object, **kwargs: float) -> MCPClientManager:
    manager = MCPClientManager(
        [MCPServerConfig(name="slow", command="python", args=[])],
        **kwargs,  # type: ignore[arg-type]
    )
    manager._sessions["slow"] = session
    return manager


class TestDeadlines:
    @pytest.mark.asyncio
    async def test_initialize_is_deadlined(self) -> None:
        manager = _manager(_HangingSession(), startup_timeout_seconds=0.05)

        with pytest.raises(MCPTimeoutError) as excinfo:
            await manager._deadline(manager._sessions["slow"].initialize(), "initialize", "slow")

        assert excinfo.value.operation == "initialize"

    @pytest.mark.asyncio
    async def test_call_tool_is_deadlined(self) -> None:
        manager = _manager(_SilentToolSession(), call_timeout_seconds=0.05)
        manager._tool_map["lookup"] = "slow"

        with pytest.raises(MCPTimeoutError) as excinfo:
            await manager.call_tool("lookup", {})

        assert excinfo.value.operation == "call_tool"
        assert excinfo.value.timeout_seconds == 0.05

    @pytest.mark.asyncio
    async def test_a_deadline_is_not_an_unbounded_wait(self) -> None:
        """The bound must actually bound — not merely exist as a parameter."""
        manager = _manager(_SilentToolSession(), call_timeout_seconds=0.05)
        manager._tool_map["lookup"] = "slow"

        with pytest.raises(MCPTimeoutError):
            await asyncio.wait_for(manager.call_tool("lookup", {}), timeout=5.0)


class TestStartFailureStopsWhatItStarted:
    """Asserted against the real runner source, not a local mirror of it.

    A mirror would prove only that ``try``/``finally`` works in Python. The
    defect was the *placement* of one call relative to one ``try``, so the
    property to check is a structural fact about ``runner.py`` itself.

    The property is "a start that raises is followed by a stop", not "the start
    sits in a try whose ``finally`` stops". An earlier version of this test
    asserted the second, which is one *implementation* of the first -- and it
    would have rejected the tighter shape that closes the same leak by giving
    the start its own handler. A guard that fails a correct fix is a worse guard.
    """

    @staticmethod
    def _runner_tree() -> ast.AST:
        from zeroth.runtime.agents import runner as runner_module

        return ast.parse(inspect.getsource(runner_module))

    @staticmethod
    def _calls(node: ast.AST, name: str) -> bool:
        return any(
            isinstance(inner, ast.Attribute) and inner.attr == name for inner in ast.walk(node)
        )

    @classmethod
    def _stops_on_failure(cls, node: ast.Try) -> bool:
        """Whether *node* stops the servers on any non-success exit.

        Either arrangement qualifies: a ``finally`` that always stops, or an
        ``except`` that stops before re-raising. Both leave nothing entered.
        """
        cleanup = list(node.finalbody) + [stmt for h in node.handlers for stmt in h.body]
        return any(cls._calls(stmt, "_stop_mcp_servers") for stmt in cleanup)

    def test_every_mcp_start_is_guarded_by_a_stop(self) -> None:
        tree = self._runner_tree()

        starts = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Await) and self._calls(node, "_start_mcp_servers")
        ]
        assert starts, "runner.py no longer starts any MCP servers -- test is stale"

        guarding = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Try)
            and self._stops_on_failure(node)
            and any(self._calls(stmt, "_start_mcp_servers") for stmt in node.body)
        ]

        assert len(guarding) >= len(starts), (
            f"{len(starts)} MCP start(s) but only {len(guarding)} guarded by a stop -- "
            "a start that raises partway through leaks every server it already entered"
        )

    def test_the_guard_would_notice_an_unguarded_start(self) -> None:
        """The detector, fed the arrangement it exists to reject.

        Without this the assertion above could hold because the walk finds
        nothing at all, which is the failure mode a structural test invites.
        """
        unguarded = ast.parse(
            "async def run(self):\n"
            "    await self._start_mcp_servers(caps)\n"
            "    try:\n"
            "        await self._work()\n"
            "    finally:\n"
            "        await self._stop_mcp_servers()\n"
        )

        starts = [
            node
            for node in ast.walk(unguarded)
            if isinstance(node, ast.Await) and self._calls(node, "_start_mcp_servers")
        ]
        guarding = [
            node
            for node in ast.walk(unguarded)
            if isinstance(node, ast.Try)
            and self._stops_on_failure(node)
            and any(self._calls(stmt, "_start_mcp_servers") for stmt in node.body)
        ]

        assert len(starts) == 1
        assert guarding == [], "the detector accepted a start outside every stopping try"

    def test_the_guard_accepts_an_except_that_stops(self) -> None:
        """The shape this branch merged from main must be accepted, not rejected."""
        guarded_by_handler = ast.parse(
            "async def run(self):\n"
            "    try:\n"
            "        await self._start_mcp_servers(caps)\n"
            "    except Exception:\n"
            "        await self._stop_mcp_servers()\n"
            "        raise\n"
        )

        guarding = [
            node
            for node in ast.walk(guarded_by_handler)
            if isinstance(node, ast.Try)
            and self._stops_on_failure(node)
            and any(self._calls(stmt, "_start_mcp_servers") for stmt in node.body)
        ]

        assert len(guarding) == 1
