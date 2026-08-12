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

import asyncio

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

    A mirror would prove only that ``try``/``finally`` works in Python.  The
    defect was the *placement* of one call relative to one ``try``, so the
    property to check is a structural fact about ``runner.py`` itself.
    """

    def test_the_start_call_is_inside_the_stopping_try(self) -> None:
        import ast
        import inspect

        from zeroth.runtime.agents import runner as runner_module

        tree = ast.parse(inspect.getsource(runner_module))

        def _calls(node: ast.AST, name: str) -> bool:
            return any(
                isinstance(inner, ast.Attribute) and inner.attr == name for inner in ast.walk(node)
            )

        guarded = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Try)
            and node.finalbody
            and any(_calls(stmt, "_stop_mcp_servers") for stmt in node.finalbody)
            and any(_calls(stmt, "_start_mcp_servers") for stmt in node.body)
        ]

        assert guarded, (
            "no try/finally in runner.py both starts the MCP servers in its body "
            "and stops them in its finally -- a start that raises partway through "
            "leaks every server it already entered"
        )

        stray = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Await) and _calls(node, "_start_mcp_servers")
        ]
        assert len(stray) == len(guarded), (
            "an MCP start is awaited outside the try that stops the servers"
        )
