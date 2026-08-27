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


class TestPoolLifecycleIsGuarded:
    """The same bound, retargeted at the pool that now owns MCP sessions.

    The runner guard above still covers the legacy ``mcp_servers`` path. This
    covers the new owner: ``_drive`` creates a pool per run, so a run that
    fails or pauses at an approval gate must still stop every process it
    started. Without this, moving ownership would have quietly dropped the
    guarantee the runner test was written to hold.

    The correlation is what carries this. An earlier version looked for *a*
    pool creation anywhere in the module and *a* try/finally that stopped
    *something* anywhere else, which is satisfied by a module where the two have
    nothing to do with each other -- and the pool would leak exactly as before.
    The property is: the name the pool is bound to is the name a ``finally`` in
    the same function stops.
    """

    @staticmethod
    def _orchestrator_tree() -> ast.AST:
        # Resolved through the imported module rather than a cwd-relative path:
        # a guard that silently reads nothing when pytest is invoked from
        # anywhere but the repo root is a guard that stops guarding without
        # saying so.
        from zeroth.runtime.orchestration import orchestrator

        return ast.parse(inspect.getsource(orchestrator))

    @staticmethod
    def _mentions(node: ast.AST, name: str) -> bool:
        return any(
            (isinstance(inner, ast.Attribute) and inner.attr == name)
            or (isinstance(inner, ast.Name) and inner.id == name)
            for inner in ast.walk(node)
        )

    @classmethod
    def _stops_the_name(cls, stmt: ast.AST, name: str) -> bool:
        """Whether *stmt* calls ``.stop()`` on the variable *name* itself."""
        for inner in ast.walk(stmt):
            if not isinstance(inner, ast.Call):
                continue
            func = inner.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "stop"
                and isinstance(func.value, ast.Name)
                and func.value.id == name
            ):
                return True
        return False

    @classmethod
    def _unguarded_pools(cls, tree: ast.AST) -> list[str]:
        """Names bound to an ``MCPSessionPool`` that nothing in scope stops.

        Returns the offending variable names, so a failure says which pool.
        """
        offenders: list[str] = []
        for scope in ast.walk(tree):
            if not isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for stmt in ast.walk(scope):
                if not isinstance(stmt, ast.Assign):
                    continue
                call = stmt.value
                if not (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id == "MCPSessionPool"
                ):
                    continue
                targets = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
                if not targets:
                    offenders.append("<non-name target>")
                    continue
                name = targets[0]
                stopped = any(
                    isinstance(node, ast.Try)
                    and any(cls._stops_the_name(final, name) for final in node.finalbody)
                    for node in ast.walk(scope)
                )
                if not stopped:
                    offenders.append(name)
        return offenders

    def test_every_pool_creation_is_guarded_by_a_stop(self) -> None:
        tree = self._orchestrator_tree()

        creations = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "MCPSessionPool"
        ]
        assert creations, (
            "the orchestrator no longer creates an MCPSessionPool -- ownership moved "
            "again and this guard is stale"
        )

        assert self._unguarded_pools(tree) == [], (
            "a run creates MCP sessions with no finally that stops that same pool -- a "
            "run that fails or pauses at an approval gate would leak every process it "
            "started"
        )

        drive_guarded = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Try)
            and any(self._mentions(stmt, "stop") for stmt in node.finalbody)
            and any(self._mentions(stmt, "drive") for stmt in node.body)
        ]
        assert drive_guarded, "the guarded region does not cover the run's drive loop"

    def test_the_guard_would_notice_a_finally_that_stops_something_else(self) -> None:
        """The detector, fed the arrangement it exists to reject.

        A ``finally`` that stops *a* thing is not a ``finally`` that stops *the
        pool*, and the uncorrelated form of this check accepted the difference.
        """
        decoy = ast.parse(
            "async def _drive(self):\n"
            "    pool = MCPSessionPool(self.mcp_server_resolver)\n"
            "    try:\n"
            "        return await self._driver.drive(graph, run)\n"
            "    finally:\n"
            "        await self._tracer.stop()\n"
        )
        assert self._unguarded_pools(decoy) == ["pool"], (
            "the detector accepted a finally that stops an unrelated object"
        )

    def test_the_guard_accepts_the_real_arrangement(self) -> None:
        """And is not merely rejecting everything.

        Kept in step with the teardown the orchestrator actually writes. This
        decoy said ``contextlib.suppress(Exception)`` for as long as that was
        the shape, and a fixture describing an arrangement the source no longer
        has is the same defect as a comment that does.
        """
        correct = ast.parse(
            "async def _drive(self):\n"
            "    pool = MCPSessionPool(self.mcp_server_resolver)\n"
            "    try:\n"
            "        return await self._driver.drive(graph, run)\n"
            "    finally:\n"
            "        try:\n"
            "            await pool.stop()\n"
            "        except Exception:\n"
            "            logger.exception('did not stop cleanly')\n"
        )
        assert self._unguarded_pools(correct) == []

    # "A stop failure must not mask why the run ended" used to be checked here,
    # by walking the orchestrator's source for the token ``suppress`` inside the
    # teardown's ``finally``. That matched a representation of the property
    # rather than the property, in both directions. It passed for
    # ``contextlib.suppress(Exception)``, which does hold the run's outcome --
    # and also discarded the only evidence that a session had been stranded, so
    # a cross-task close failure on every parallel run read as a clean teardown.
    # And it failed the moment that was replaced by an equivalent ``try/except``
    # that logs, which holds the outcome *and* keeps the evidence.
    #
    # The property is now asserted where it can be observed rather than
    # recognised: ``test_mcp_end_to_end.py::
    # test_a_teardown_failure_does_not_replace_the_runs_own_outcome`` drives a
    # real orchestrator with a pool whose ``stop`` raises, and asserts both
    # halves -- the run still reports its own result, and the failure is logged.


class TestTheRuntimeResolverIsWired:
    """A pool that is never constructed enforces nothing.

    The publish-time ceiling shipped unwired once already: the check existed,
    every test passed, and no production caller reached it. This pins the
    runtime half so the same class of gap cannot recur silently.
    """

    @staticmethod
    def _unresolved_orchestrators(tree: ast.AST) -> list[str]:
        """Reasons each ``RuntimeOrchestrator(...)`` fails to wire a resolver.

        Passing the keyword is not the property; passing something is. The
        orchestrator's own ``_drive`` returns early when the resolver is
        ``None``, so ``mcp_server_resolver=None`` satisfies "the keyword is
        present" while creating no pool at all -- which is the shipped-unwired
        shape this guard exists to catch, dressed up to pass it.
        """
        reasons: list[str] = []
        for call in ast.walk(tree):
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "RuntimeOrchestrator"
            ):
                continue
            keywords = {kw.arg: kw.value for kw in call.keywords}
            if "mcp_server_resolver" not in keywords:
                reasons.append("keyword missing")
                continue
            value = keywords["mcp_server_resolver"]
            if isinstance(value, ast.Constant) and value.value is None:
                reasons.append("mcp_server_resolver=None")
        return reasons

    def test_bootstrap_hands_the_orchestrator_an_mcp_server_resolver(self) -> None:
        from zeroth.service.bootstrap import factory as bootstrap_factory

        tree = ast.parse(inspect.getsource(bootstrap_factory))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "RuntimeOrchestrator"
        ]
        assert calls, "no RuntimeOrchestrator construction found in the bootstrap factory"
        assert self._unresolved_orchestrators(tree) == [], (
            "bootstrap builds a RuntimeOrchestrator with no usable mcp_server_resolver; "
            "no pool is ever created and every mcp_tool call fails closed"
        )

    def test_the_guard_rejects_a_resolver_that_is_literally_none(self) -> None:
        """The detector, fed the arrangement it exists to reject."""
        wired_to_nothing = ast.parse("orch = RuntimeOrchestrator(mcp_server_resolver=None)")
        assert self._unresolved_orchestrators(wired_to_nothing) == ["mcp_server_resolver=None"]

        omitted = ast.parse("orch = RuntimeOrchestrator(graph_repository=repo)")
        assert self._unresolved_orchestrators(omitted) == ["keyword missing"]

        real = ast.parse("orch = RuntimeOrchestrator(mcp_server_resolver=resolve_mcp_server)")
        assert self._unresolved_orchestrators(real) == []
