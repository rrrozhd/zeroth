"""Run-scoped pooling and gating for MCP server sessions.

One process per distinct ``server_ref`` per run, spawned on the first tool call
that needs it and stopped once when the run ends. Ownership sits here rather
than on the agent runner because two agents in one run referencing the same
server should share a process instead of each paying a spawn and a handshake.

The pooling is what forces the gate's shape. If the capability check happened
at spawn time, a second ``mcp_tool`` node reusing a session the first one
started would never meet a gate at all -- so the check is per *call*, ahead of
the session lookup, and never per server first spawn.

A session is not held by whichever call happened to ask for it first; it lives
on a task of its own. See :class:`_SessionOwner` -- the reason is a hard anyio
constraint, and getting it wrong leaked a process on every parallel run.

Two subjects meet here and they are not the same node. The **ceiling** is the
operator's assertion about the server, so its subject is the ``mcp_tool`` node
whose declared ``capability_bindings`` say what that attachment will do. The
**floor** is about the caller, so its subject is the agent node whose effective
capabilities say what the run granted it. Conflating them is what let a widened
grant be demanded for capabilities an agent held for unrelated reasons.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from zeroth.governance.policy.errors import require_capabilities
from zeroth.governance.policy.models import Capability
from zeroth.runtime.agents.mcp import (
    MCP_REQUIRED_CAPABILITIES,
    MCPClientManager,
    MCPSchemaDriftError,
    RegisteredMCPServerConfig,
    tool_schema_hash,
)

logger = logging.getLogger(__name__)

__all__ = [
    "MCPCeilingExceededError",
    "MCPServerResolver",
    "MCPSessionPool",
    "MCPToolDispatchError",
    "UnknownMCPServerError",
]


class MCPServerResolver(Protocol):
    """Resolves a ``server_ref`` to the operator's registered configuration."""

    async def __call__(self, server_ref: str) -> RegisteredMCPServerConfig | None:
        """Return the registration, or ``None`` if this deployment has none."""


class MCPCeilingExceededError(RuntimeError):
    """An ``mcp_tool`` node declared more than the operator granted its server."""

    def __init__(self, server_ref: str, tool_node_id: str, excess: list[str]) -> None:
        super().__init__(
            f"mcp_tool node {tool_node_id!r} declares {', '.join(excess)}, which MCP "
            f"server {server_ref!r} does not grant; an operator must widen the "
            "server's grants or the node must declare less"
        )
        self.server_ref = server_ref
        self.tool_node_id = tool_node_id
        self.excess = excess


class UnknownMCPServerError(RuntimeError):
    """A graph referenced a server this deployment does not have registered."""

    def __init__(self, server_ref: str) -> None:
        super().__init__(f"unknown MCP server {server_ref!r}")
        self.server_ref = server_ref


class MCPToolDispatchError(RuntimeError):
    """The transport call reached the server and failed there.

    This type is a **positive** statement about how far the call got, not a
    classification of what went wrong. Everything the pool raises before
    ``call_tool`` -- unknown server, capability denial, ceiling, a spawn that
    never handshook, schema drift -- means the tool was never invoked and
    nothing happened. Once ``call_tool`` is entered that guarantee is gone: an
    MCP call is at-least-once with no replay suppression and no reconciliation,
    so a timeout or a broken pipe may well have executed the effect.

    Callers use the type to decide whether the at-least-once marker belongs on
    the audit record. Deriving that from the *failure* instead -- "these error
    types mean it did not run" -- is what let the marker go missing: every new
    failure mode defaults to the wrong side of a negative test.
    """

    def __init__(
        self, server_ref: str, tool_name: str, cause: BaseException | None = None
    ) -> None:
        detail = f": {cause}" if cause is not None else ""
        super().__init__(
            f"MCP tool {tool_name!r} on server {server_ref!r} failed after dispatch{detail}; "
            "the call may already have taken effect"
        )
        self.server_ref = server_ref
        self.tool_name = tool_name


@dataclass(slots=True)
class _SessionOwner:
    """The task holding one server's session open, and the switch that ends it.

    A session cannot simply be opened wherever the first call happens to run.
    ``MCPClientManager`` keeps its transport in an ``AsyncExitStack``, and the
    ``stdio_client`` inside it enters an anyio cancel scope bound to *the task
    that entered it*; closing that stack from any other task raises "Attempted
    to exit cancel scope in a different task than it was entered in". Under
    fan-out the first call to a server arrives inside a branch task created by
    ``gather``, and ``stop`` runs later on the run's own task -- so the close
    failed, and the orchestrator's ``suppress(Exception)`` turned that into a
    silently leaked process for every parallel run.

    Giving the session its own task makes open and close the same task by
    construction, whoever happens to ask for it first.
    """

    task: asyncio.Task[None]
    closing: asyncio.Event


class MCPSessionPool:
    """Lazily spawns and shares MCP sessions for the lifetime of one run."""

    def __init__(self, resolver: MCPServerResolver) -> None:
        self._resolver = resolver
        self._managers: dict[str, MCPClientManager] = {}
        #: Registrations resolved this run, so the ceiling is read once.
        self._configs: dict[str, RegisteredMCPServerConfig] = {}
        #: Tool name -> pinned hash, per server, as advertised at first spawn.
        self._live_hashes: dict[str, dict[str, str]] = {}
        #: One spawn at a time per server, so racing branches share a process.
        self._spawn_locks: dict[str, asyncio.Lock] = {}
        #: The task that opened each session and will close it.
        self._owners: dict[str, _SessionOwner] = {}

    async def call(
        self,
        *,
        server_ref: str,
        tool_name: str,
        arguments: dict[str, Any],
        agent_node_id: str,
        tool_node_id: str,
        declared_capabilities: set[Capability],
        effective_capabilities: set[Capability] | None,
        pinned_hash: str,
    ) -> Any:
        """Gate, spawn if needed, verify the pin, then call the tool."""
        config = await self._config_for(server_ref)
        self._require_capabilities(
            config,
            agent_node_id=agent_node_id,
            tool_node_id=tool_node_id,
            declared_capabilities=declared_capabilities,
            effective_capabilities=effective_capabilities,
        )
        await self._session_for(config)
        self._require_pin(server_ref, tool_name, pinned_hash)
        manager = self._managers[server_ref]
        try:
            return await manager.call_tool(tool_name, arguments)
        except Exception as exc:
            # Everything above this line means "no effect happened"; from here
            # it does not, so the failure changes type to say so. Cancellation
            # is deliberately not wrapped -- turning a ``CancelledError`` into a
            # ``RuntimeError`` would swallow the cancellation itself.
            raise MCPToolDispatchError(server_ref, tool_name, exc) from exc

    async def _config_for(self, server_ref: str) -> RegisteredMCPServerConfig:
        """Resolve the operator's registration, cached for the run."""
        cached = self._configs.get(server_ref)
        if cached is not None:
            return cached
        config = await self._resolver(server_ref)
        if config is None:
            raise UnknownMCPServerError(server_ref)
        self._configs[server_ref] = config
        return config

    def _require_capabilities(
        self,
        config: RegisteredMCPServerConfig,
        *,
        agent_node_id: str,
        tool_node_id: str,
        declared_capabilities: set[Capability],
        effective_capabilities: set[Capability] | None,
    ) -> None:
        """Deny before a process exists, on every call, and never above the ceiling.

        Spawning is a side effect: denying only at call time would leave the
        process already started. And because sessions are shared, gating on
        first *spawn* would let every caller after the first ride in on the
        first one's grant -- so this runs ahead of ``_session_for``, per call.

        There is deliberately no memo. An earlier version remembered which
        (server, tool node, agent) triples had cleared and returned early for
        the rest of the run, which quietly turned the paragraph below into a
        first-call-only check: the ceiling was described as re-evaluated at run
        time and was in fact evaluated once. Both comparisons are set
        differences over a handful of enum members against a config already
        cached for the run, so remembering the answer bought nothing and cost
        the property the docstring claims.

        **The ceiling's subject is the ``mcp_tool`` node**, and it is checked
        unconditionally. ``caps(M)`` is static graph data and the grants are
        operator-owned, so neither depends on capability enforcement being
        active for this run; skipping the ceiling in advisory mode would make
        the operator's assertion about the server contingent on a policy switch
        it has nothing to do with. It also cannot be left to publish alone: a
        published graph version is immutable, so a node validated against
        yesterday's grants would otherwise keep capabilities the operator has
        since withdrawn, and this is the only check standing if a deployment
        ever constructs its validator without the grants resolver.

        The **floor's** subject is the agent, and it is skipped when
        ``effective_capabilities`` is ``None`` -- the runner's convention for
        "enforcement is not wired". It restates what the tool gate already
        requires of the agent, kept here as the one gate still standing if that
        one is ever bypassed.

        What is deliberately *not* here is ``effective(A) - grants``. Measuring
        the agent against the server's ceiling made an agent's unrelated
        capabilities -- filesystem access it holds for some other tool -- into
        something an operator had to grant the MCP server before the agent
        could call it at all. Following that error message converges every
        server's grants on the union of everything any agent holds, which is
        the control dissolving itself.
        """
        granted = set(config.grants)
        excess = sorted(cap.value for cap in (set(declared_capabilities) - granted))
        if excess:
            raise MCPCeilingExceededError(config.name, tool_node_id, excess)
        if effective_capabilities is not None:
            require_capabilities(
                MCP_REQUIRED_CAPABILITIES,
                effective_capabilities,
                node_id=agent_node_id,
            )

    def _session_is_ready(self, server_ref: str) -> bool:
        """Whether a session exists *and* finished its handshake.

        Membership in ``_managers`` is not readiness. The owner registers its
        manager before awaiting the handshake -- deliberately, so a spawn that
        fails is never unreachable -- which leaves a window where the entry
        exists and its tools have not been pinned yet. A concurrent caller that
        read that entry as a live session sailed past the spawn and then found
        every tool absent, i.e. reported schema drift against a server that had
        not said anything yet. The pinned hashes are written only on a completed
        start, so they are the honest readiness signal.
        """
        return server_ref in self._live_hashes

    async def _session_for(self, config: RegisteredMCPServerConfig) -> None:
        """Return an already-running session, or spawn one and pin its tools.

        Serialised per server. ``RuntimeParallelExecutor`` fans branches out
        with ``create_task``/``gather`` and every branch of one run shares this
        pool, so an unlocked check-then-spawn had all of them miss the
        membership test together: each spawned its own process, the last write
        won, and the rest were never reachable by ``stop``. That falsifies the
        module's headline guarantee exactly where the runtime fans out.

        The caller only ever *waits* for a session; it never holds one. See
        :class:`_SessionOwner` for why the session must live on a task of its
        own rather than on whichever branch asked for it first.
        """
        server_ref = config.name
        if self._session_is_ready(server_ref):
            return
        lock = self._spawn_locks.setdefault(server_ref, asyncio.Lock())
        async with lock:
            # Re-checked inside the lock: the racing callers that queued behind
            # the winner must find its session rather than start another.
            if self._session_is_ready(server_ref):
                return
            ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            closing = asyncio.Event()
            owner = _SessionOwner(
                task=asyncio.create_task(self._own_session(config, ready, closing)),
                closing=closing,
            )
            self._owners[server_ref] = owner
            try:
                await ready
            except BaseException:  # noqa: BLE001 - re-raised below
                # A spawn that failed has already unwound itself; a *cancelled*
                # wait may leave the owner about to park on a session nobody
                # will ever ask for, so it is told to close either way.
                closing.set()
                self._owners.pop(server_ref, None)
                raise

    async def _own_session(
        self,
        config: RegisteredMCPServerConfig,
        ready: asyncio.Future[None],
        closing: asyncio.Event,
    ) -> None:
        """Open one server's session, hold it, and close it in this same task.

        Everything that touches the transport happens here, which is the point:
        the anyio cancel scope ``stdio_client`` enters is entered and exited by
        one task, so neither a branch task finishing nor ``stop`` running
        elsewhere can strand it.
        """
        server_ref = config.name
        manager = MCPClientManager([config])
        # Registered *before* the handshake, not after. ``start`` enters a
        # stdio_client transport -- which is where the child is forked --
        # before the ``initialize`` it can fail on, so a manager that only
        # becomes reachable on success is a manager nothing can ever close:
        # the transport stays entered and the child outlives the run.
        self._managers[server_ref] = manager
        try:
            manifests = await manager.start()
        except BaseException as exc:  # noqa: BLE001 - handed to the waiter
            # Dropped first, so the next call retries the spawn instead of
            # treating a corpse as live; then closed, so nothing stays entered.
            # Both happen *before* the waiter is woken: a caller that resumed
            # first could observe a half-torn-down pool.
            self._managers.pop(server_ref, None)
            try:
                await manager.stop()
            except Exception as stop_exc:  # noqa: BLE001 - the spawn error wins
                logger.warning(
                    "MCP server %s: failed spawn could not be closed cleanly: %s",
                    server_ref,
                    stop_exc,
                )
            if not ready.done():
                ready.set_exception(exc)
            return

        # Pinned before the waiter is woken, because the hashes are what
        # ``_session_is_ready`` reads: signalling first would re-open the very
        # window this ordering closes, and concurrent callers would race past
        # the spawn and read every tool as absent.
        self._live_hashes[server_ref] = {
            manifest.alias: tool_schema_hash(
                manifest.alias, manifest.description, manifest.parameters_schema
            )
            for manifest in manifests
        }
        logger.info(
            "MCP server %s: session started with %d tools", server_ref, len(manifests)
        )
        if not ready.done():
            ready.set_result(None)

        try:
            await closing.wait()
        except asyncio.CancelledError:
            # Cancelled rather than asked to close. The close still has to
            # happen -- a cancelled owner that skipped it would leave the child
            # running with nothing holding a handle to it -- and the task ends
            # immediately afterwards, which is what the cancellation wanted.
            logger.warning("MCP server %s: session owner cancelled; closing anyway", server_ref)
        finally:
            self._managers.pop(server_ref, None)
            self._live_hashes.pop(server_ref, None)
            await manager.stop()

    def _require_pin(self, server_ref: str, tool_name: str, pinned_hash: str) -> None:
        """Refuse to call a tool whose live shape no longer matches the graph.

        Fail closed. Without this the pin is only a stale copy of a schema the
        server is free to change, and the publish-time validation it enables
        would be decorative.
        """
        live = self._live_hashes.get(server_ref, {}).get(tool_name)
        if live is None:
            raise MCPSchemaDriftError(server_ref, tool_name, pinned_hash, "<absent>")
        if live != pinned_hash:
            raise MCPSchemaDriftError(server_ref, tool_name, pinned_hash, live)

    async def stop(self) -> None:
        """Stop every session this run started.

        Each owner is asked to close and then awaited. One server failing to
        shut down must not strand the others, so the failures are collected
        (``return_exceptions``) rather than allowed to abandon the remaining
        owners mid-await, and the first is re-raised once they have all ended.
        """
        owners = list(self._owners.items())
        self._owners.clear()
        self._configs.clear()
        self._spawn_locks.clear()
        for _, owner in owners:
            owner.closing.set()
        results = await asyncio.gather(
            *(owner.task for _, owner in owners), return_exceptions=True
        )
        # The owners clear their own entries as they unwind; this covers an
        # owner that never got far enough to register one.
        self._managers.clear()
        self._live_hashes.clear()
        first_error: BaseException | None = None
        for (server_ref, _), result in zip(owners, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning("MCP server %s failed to stop cleanly: %s", server_ref, result)
                first_error = first_error or result
        if first_error is not None:
            raise first_error
