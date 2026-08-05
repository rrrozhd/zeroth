"""Proof that ``ZerothMiddleware`` governs a ``create_agent`` agent's tool calls.

**"Denied" is "the tool body ran zero times", and a handler count no longer says
that.** ``ZerothMiddleware`` decides nothing itself: it substitutes a governed
twin of ``request.tool`` into the request and hands it downstream, so the verdict
is reached *inside* ``handler`` and ``handler`` is called on every verdict. Every
enforcement assertion below therefore counts ``Body.calls`` -- a counter inside
the function ``StructuredTool`` actually invokes -- together with
``pytest.raises`` on the exact exception class. A suite that counted handlers
would now read ``1`` for a denial and prove nothing.

**``Handler`` is a ``ToolNode`` stand-in, not a null downstream.** It executes
whatever tool the request carries, because that is the only way the decision is
reached at all. A double that ignored ``request.tool`` would report an allow for
every scenario.

**A handler count is still the right measurement for exactly one family: the
gates that refuse a request before any twin can be installed.** An unresolved
tool, a renamed call, a hostile tool-call mapping -- those raise out of
``wrap_tool_call`` itself, so ``handler.calls == 0`` is the evidence, and it is
also the proof those identity gates survived the move of the decision.

**Nesting order is asserted, not assumed (R12).** Three middleware each append an
entry *and* an exit marker, and each records whether the request it was handed
already carried a governed tool. The marker sequence pins the nesting; the
governed/raw flag pins where in that sequence the substitution happened -- which
is what the decision's own position used to show, before the decision moved below
every layer.

**Tier A.** This suite needs ``langchain.agents``, which ships only in the
optional ``gateway-conformance`` group, so it carries both guards: an
``importorskip`` before any langchain import and the ``langgraph_conformance``
marker. ``addopts`` deselects that marker, so these tests do **not** run in the
default suite (or in the pre-commit hook); run them with
``-o addopts= -m langgraph_conformance``.

**The guard names the module this file actually imports.** A marked module is
imported at *collection*, so skipping on ``langgraph`` while importing
``langchain.agents`` errors instead of skipping in an environment that has the
first and not the second. Guarding ``langchain.agents`` covers both, because
importing it pulls ``langgraph`` in and ``importorskip`` skips on either failure.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Annotated, Any

import pytest

pytest.importorskip("langchain.agents", reason="requires the gateway-conformance dependency group")

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, ToolCallRequest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.prebuilt import InjectedState

from tests.integrations.langgraph.genai._causal import HostileStr
from tests.integrations.langgraph.tools._agents import scripted_model
from tests.integrations.langgraph.tools._hostile import HostileDict, HostileKey
from zeroth.core.langgraph_gateway.models import GovernanceLevel
from zeroth.governance.audit import NodeAuditRecord
from zeroth.integrations.langgraph._middleware import ZerothMiddleware
from zeroth.integrations.langgraph._tool_errors import (
    ApprovalRequiresThreadError,
    GovernanceContextError,
    PolicyViolation,
    ToolGovernanceError,
    UnstableToolIdentityError,
)
from zeroth.integrations.langgraph._tool_types import (
    InventoryCoverage,
    SideEffectClass,
    ToolAction,
    ToolDecision,
    ToolDecisionKind,
    ToolGovernanceContext,
)
from zeroth.integrations.langgraph._tool_wrappers import GovernedTool, govern_tools

pytestmark = pytest.mark.langgraph_conformance

THREADED = ToolGovernanceContext(
    tenant_id="tenant-a",
    principal_id="principal-1",
    run_id="run-1",
    thread_id="thread-1",
    correlation_id="corr-1",
)
"""A run that can be paused: approval needs a thread to resume into."""

THREADLESS = ToolGovernanceContext(tenant_id="tenant-a", principal_id="principal-1", run_id="run-1")

ALLOW = ToolDecision(kind=ToolDecisionKind.ALLOW, reason_code="unknown_error")
DENY = ToolDecision(kind=ToolDecisionKind.DENY, reason_code="policy_violation")
APPROVE = ToolDecision(
    kind=ToolDecisionKind.REQUIRE_APPROVAL,
    reason_code="policy_violation",
    approval_ref="approval-7",
)


class Suspended(Exception):  # noqa: N818 - a pause, not a malfunction.
    """Stands in for LangGraph's ``GraphInterrupt``, which is what a real pause raises."""


@dataclasses.dataclass
class CountingClient:
    """A decision client that returns a fixed verdict and counts every consultation."""

    verdict: object = ALLOW
    calls: int = 0
    seen: list[ToolAction] = dataclasses.field(default_factory=list)

    def decide(self, action: ToolAction, context: ToolGovernanceContext) -> ToolDecision:
        """Record the consultation and return the configured verdict."""
        self.calls += 1
        self.seen.append(action)
        return self.verdict  # type: ignore[return-value]


@dataclasses.dataclass
class PerToolClient:
    """A decision client that answers differently per tool name."""

    verdicts: dict[str, ToolDecision] = dataclasses.field(default_factory=dict)
    seen: list[str] = dataclasses.field(default_factory=list)

    def decide(self, action: ToolAction, context: ToolGovernanceContext) -> ToolDecision:
        """Return the verdict configured for *action*'s tool, defaulting to allow."""
        self.seen.append(action.identity.name)
        return self.verdicts.get(action.identity.name, ALLOW)


@dataclasses.dataclass
class LoggingClient:
    """A decision client that marks its consultation into a shared ordering log.

    It marks *where the decision happens*, which is now below every middleware --
    inside ``ToolNode``, in the governed twin. That is why it no longer locates
    ``ZerothMiddleware`` in the chain and :class:`Marker`'s governed/raw flag
    does: the marker is kept because "the decision happened once, innermost" is
    still worth pinning.
    """

    log: list[str]
    verdict: ToolDecision = ALLOW

    def decide(self, action: ToolAction, context: ToolGovernanceContext) -> ToolDecision:
        """Mark the consultation and return the configured verdict."""
        self.log.append("z:decide")
        return self.verdict


@dataclasses.dataclass
class RecordingInterrupt:
    """A pause seam that records the payload and suspends by raising, as LangGraph does."""

    payloads: list[Any] = dataclasses.field(default_factory=list)

    def __call__(self, payload: Any) -> Any:
        """Record *payload* and suspend."""
        self.payloads.append(payload)
        raise Suspended


@dataclasses.dataclass
class RecordingSubmitter:
    """An audit sink that keeps every record the enforcement core handed it."""

    records: list[NodeAuditRecord] = dataclasses.field(default_factory=list)

    def submit(self, record: NodeAuditRecord) -> None:
        """Keep *record*, as the delivery queue's non-blocking hand-off does."""
        self.records.append(record)


@dataclasses.dataclass
class Handler:
    """``ToolNode`` as the middleware meets it: it executes whatever tool the request carries.

    The middleware substitutes a governed twin into ``request.tool``, so a
    downstream that returned a canned value would never reach a decision. The raw
    argument mapping is handed to ``invoke`` -- rather than the whole tool call --
    so an allowed call returns the body's own value and the assertions stay about
    governance rather than about ``ToolMessage`` shapes; the real tool-call path
    is exercised by the ``create_agent`` tests further down.
    """

    calls: int = 0
    seen: list[Any] = dataclasses.field(default_factory=list)

    def __call__(self, request: Any) -> Any:
        """Count this invocation and execute the tool the request now carries."""
        self.calls += 1
        self.seen.append(request)
        return request.tool.invoke(dict(request.tool_call["args"]))

    async def acall(self, request: Any) -> Any:
        """The awaitable form of the same downstream."""
        self.calls += 1
        self.seen.append(request)
        await asyncio.sleep(0)
        return await request.tool.ainvoke(dict(request.tool_call["args"]))


def read_only(_target: object) -> SideEffectClass:
    """Classify every tool as read-only, so the allow path needs no blanket opt-in."""
    return SideEffectClass.READ_ONLY


BODY_RESULT = "body-result"
"""What an allowed call returns, once it has travelled back out of the downstream."""


@dataclasses.dataclass
class Body:
    """A tool body that counts every execution, and can fail like a real one.

    An ``Exception`` in *result* is raised rather than returned, which is how a
    downstream failure is staged now: the handler no longer stands in for the
    tool, so "the tool blew up" has to happen where the tool is.
    """

    result: Any = BODY_RESULT
    calls: int = 0

    def run(self, **_kwargs: Any) -> Any:
        """Count this execution and return -- or raise -- the configured result."""
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def build_tool(name: str = "search", body: Body | None = None) -> StructuredTool:
    """Build a sync ``BaseTool`` with a declared schema."""
    target = body or Body()

    def _run(query: str) -> str:
        return target.run(query=query)

    return StructuredTool.from_function(func=_run, name=name, description=f"{name} for something.")


def build_async_tool(name: str = "asearch", body: Body | None = None) -> StructuredTool:
    """Build an async ``BaseTool`` with a declared schema."""
    target = body or Body()

    async def _arun(query: str) -> str:
        return target.run(query=query)

    return StructuredTool.from_function(
        coroutine=_arun, name=name, description=f"{name} for something."
    )


def build_untyped_tool(name: str = "search", body: Body | None = None) -> StructuredTool:
    """Build a tool whose argument is ``Any``, so validation admits anything at all.

    The seam that lets a test reach the *canonical projection* now that validation
    runs first: a typed field would refuse an unrepresentable value before
    governance ever saw it.
    """
    target = body or Body()

    def _run(payload: Any) -> str:
        return target.run(payload=payload)

    return StructuredTool.from_function(func=_run, name=name, description=f"{name} for something.")


def build_request(
    tool: Any, *, name: str | None = None, args: Any = None, call_id: str = "call-1"
) -> ToolCallRequest:
    """Build the middleware request LangChain would hand in for one tool call."""
    return ToolCallRequest(
        tool_call={
            "name": tool.name if name is None else name,
            "args": {"query": "cats"} if args is None else args,
            "id": call_id,
            "type": "tool_call",
        },
        tool=tool,
        state={},
        runtime=None,
    )


class Impostor:
    """A request-shaped object that is not a ``ToolCallRequest``.

    The middleware reads a request structurally, so the gates have to hold for
    anything shaped like one -- a hostile object cannot be kept out by type alone
    without also rejecting ``request.override()``'s legitimate replacements.
    """

    def __init__(self, tool_call: Any, tool: Any) -> None:
        self.tool_call = tool_call
        self.tool = tool
        self.state: Any = {}
        self.runtime: Any = None


def middleware(**overrides: Any) -> ZerothMiddleware:
    """Build a middleware whose default seams allow a classified read-only call."""
    seams: dict[str, Any] = {
        "context": THREADED,
        "client": CountingClient(),
        "side_effect": read_only,
    }
    seams.update(overrides)
    return ZerothMiddleware(**seams)


# --------------------------------------------------------------------------- #
# R9 / R10 -- the body is the downstream, and its execution count is the evidence.
# --------------------------------------------------------------------------- #


def test_an_allowed_call_runs_the_tool_body_exactly_once() -> None:
    body = Body()
    client = CountingClient(verdict=ALLOW)
    guard = middleware(client=client)

    result = guard.wrap_tool_call(build_request(build_tool(body=body)), Handler())

    assert result == BODY_RESULT
    assert body.calls == 1
    assert client.calls == 1


def test_a_denied_call_runs_the_tool_body_zero_times() -> None:
    body = Body()
    handler = Handler()
    guard = middleware(client=CountingClient(verdict=DENY))

    with pytest.raises(PolicyViolation):
        guard.wrap_tool_call(build_request(build_tool(body=body)), handler)

    assert body.calls == 0
    # The verdict now travels back up *through* the downstream, which is the
    # whole shape of the fix: the handler is reached, and the tool still is not.
    assert handler.calls == 1


def test_an_allowed_async_call_awaits_the_tool_body_exactly_once() -> None:
    body = Body()
    guard = middleware()

    result = asyncio.run(
        guard.awrap_tool_call(build_request(build_async_tool(body=body)), Handler().acall)
    )

    assert result == BODY_RESULT
    assert body.calls == 1


def test_a_denied_async_call_runs_the_tool_body_zero_times() -> None:
    body = Body()
    guard = middleware(client=CountingClient(verdict=DENY))

    with pytest.raises(PolicyViolation):
        asyncio.run(
            guard.awrap_tool_call(build_request(build_async_tool(body=body)), Handler().acall)
        )

    assert body.calls == 0


def test_an_approval_suspends_before_the_tool_body_is_reached() -> None:
    body = Body()
    pause = RecordingInterrupt()
    guard = middleware(client=CountingClient(verdict=APPROVE), interrupt=pause)

    with pytest.raises(Suspended):
        guard.wrap_tool_call(build_request(build_tool(body=body)), Handler())

    assert body.calls == 0
    assert len(pause.payloads) == 1
    assert pause.payloads[0]["approval_ref"] == "approval-7"
    assert pause.payloads[0]["tool_call_id"] == "call-1"


def test_middleware_preserves_the_stable_tool_call_id() -> None:
    client = CountingClient(verdict=ALLOW)
    guard = middleware(client=client)

    guard.wrap_tool_call(
        build_request(build_tool(), call_id="call-stable-7"),
        Handler(),
    )

    assert client.seen[0].tool_call_id == "call-stable-7"


def test_middleware_allows_an_idless_non_approval_call() -> None:
    body = Body()
    client = CountingClient(verdict=ALLOW)
    request = build_request(build_tool(body=body))
    request.tool_call["id"] = None

    assert middleware(client=client).wrap_tool_call(request, Handler()) == BODY_RESULT

    assert body.calls == 1
    assert client.calls == 1
    assert client.seen[0].tool_call_id is None


@pytest.mark.parametrize("call_id", ["", "  call-1  ", HostileStr("call-1")])
def test_middleware_refuses_an_unstable_tool_call_id(call_id: object) -> None:
    request = build_request(build_tool())
    request.tool_call["id"] = call_id
    handler = Handler()

    with pytest.raises(UnstableToolIdentityError):
        middleware().wrap_tool_call(request, handler)

    assert handler.calls == 0


def test_an_async_approval_suspends_before_the_tool_body_is_reached() -> None:
    body = Body()
    pause = RecordingInterrupt()
    guard = middleware(client=CountingClient(verdict=APPROVE), interrupt=pause)

    with pytest.raises(Suspended):
        asyncio.run(
            guard.awrap_tool_call(build_request(build_async_tool(body=body)), Handler().acall)
        )

    assert body.calls == 0
    assert len(pause.payloads) == 1


def test_an_approval_without_a_thread_never_reaches_the_tool_body() -> None:
    body = Body()
    pause = RecordingInterrupt()
    guard = middleware(context=THREADLESS, client=CountingClient(verdict=APPROVE), interrupt=pause)

    with pytest.raises(ApprovalRequiresThreadError):
        guard.wrap_tool_call(build_request(build_tool(body=body)), Handler())

    assert body.calls == 0
    assert pause.payloads == []


def test_a_tool_error_propagates_unchanged_and_is_never_retried() -> None:
    body = Body(result=RuntimeError("tool-blew-up"))
    guard = middleware()

    with pytest.raises(RuntimeError, match="tool-blew-up"):
        guard.wrap_tool_call(build_request(build_tool(body=body)), Handler())

    assert body.calls == 1


# --------------------------------------------------------------------------- #
# Fail-closed request handling: nothing shaped like a request slips an allow.
#
# These split into two families, and the split is the evidence that constraint B
# was honoured. The *identity gates* -- an unresolved tool, a renamed call, a
# hostile tool-call mapping, a tool whose entry path cannot be executed past --
# still refuse inside ``wrap_tool_call`` itself, before a twin can be installed,
# so they assert ``handler.calls == 0``. Everything decided by policy now refuses
# below the handler, so it asserts ``body.calls == 0`` instead. A test in the
# wrong family here is a test measuring the wrong layer.
# --------------------------------------------------------------------------- #


def test_a_call_with_no_governance_context_never_runs_the_tool_body() -> None:
    body = Body()
    guard = ZerothMiddleware(client=CountingClient(), side_effect=read_only)

    with pytest.raises(GovernanceContextError):
        guard.wrap_tool_call(build_request(build_tool(body=body)), Handler())

    assert body.calls == 0


def test_an_unregistered_tool_never_reaches_the_handler() -> None:
    handler = Handler()
    guard = middleware()
    unresolved = ToolCallRequest(
        tool_call={"name": "search", "args": {"query": "cats"}, "id": "call-1"},
        tool=None,
        state={},
        runtime=None,
    )

    with pytest.raises(UnstableToolIdentityError):
        guard.wrap_tool_call(unresolved, handler)

    assert handler.calls == 0


def test_a_tool_call_naming_a_different_tool_never_reaches_the_handler() -> None:
    handler = Handler()
    guard = middleware()

    with pytest.raises(UnstableToolIdentityError):
        guard.wrap_tool_call(build_request(build_tool("search"), name="delete_row"), handler)

    assert handler.calls == 0


def test_a_hostile_tool_call_mapping_never_reaches_the_handler() -> None:
    """A mapping that answers one thing to iteration and another to a lookup is refused."""
    handler = Handler()
    guard = middleware()
    tool = build_tool()
    hostile = HostileDict({"name": tool.name, "args": {"query": "cats"}, "id": "call-1"})

    with pytest.raises(UnstableToolIdentityError):
        guard.wrap_tool_call(Impostor(hostile, tool), handler)

    assert handler.calls == 0


def test_a_hostile_requested_name_never_reaches_the_handler() -> None:
    """A ``str`` subclass cannot pose as the resolved tool's name."""
    handler = Handler()
    guard = middleware()
    tool = build_tool()

    with pytest.raises(UnstableToolIdentityError):
        guard.wrap_tool_call(
            Impostor({"name": HostileStr(tool.name), "args": {"query": "c"}}, tool), handler
        )

    assert handler.calls == 0


def test_a_hostile_tool_call_key_never_reaches_the_handler() -> None:
    """A key whose ``__eq__`` answers to everything must not claim the name slot.

    Left ungated it would match ``name`` first, leave ``args`` unread, and get the
    call decided as an empty one while the tool ran with the real arguments.
    """
    handler = Handler()
    guard = middleware()
    tool = build_tool()

    with pytest.raises(UnstableToolIdentityError):
        guard.wrap_tool_call(Impostor({HostileKey(): tool.name}, tool), handler)

    assert handler.calls == 0


def test_unrepresentable_arguments_never_run_the_tool_body() -> None:
    """A value no canonical projection can hold refuses the call rather than eliding it.

    The tool declares its argument as ``Any``, so ``BaseTool`` validation *accepts*
    an arbitrary object and the refusal has to come from governance. A ``str``-typed
    field would be rejected by pydantic first, which would prove validation works
    and nothing about the canonical projection.
    """
    body = Body()
    guard = middleware()

    with pytest.raises(ToolGovernanceError):
        guard.wrap_tool_call(
            build_request(build_untyped_tool(body=body), args={"payload": object()}),
            Handler(),
        )

    assert body.calls == 0


def test_an_unclassified_tool_is_denied_by_default() -> None:
    """No classifier means unknown, and unknown is refused without consulting a client."""
    body = Body()
    guard = ZerothMiddleware(context=THREADED, client=CountingClient())

    with pytest.raises(PolicyViolation):
        guard.wrap_tool_call(build_request(build_tool(body=body)), Handler())

    assert body.calls == 0


# --------------------------------------------------------------------------- #
# R8 -- one enforcement core, so one tool is one identity on both surfaces.
# --------------------------------------------------------------------------- #


def test_one_tool_fingerprints_identically_on_both_install_surfaces() -> None:
    tool = build_tool()
    client = CountingClient()
    guard = middleware(client=client)

    guard.wrap_tool_call(build_request(tool), Handler())
    [governed] = govern_tools([tool], context=THREADED, client=client, side_effect=read_only)
    governed.invoke({"query": "cats"})

    middleware_action, wrapper_action = client.seen
    assert middleware_action.identity == wrapper_action.identity
    assert dict(middleware_action.arguments) == dict(wrapper_action.arguments)


def test_the_decision_record_carries_the_tool_on_the_typed_field() -> None:
    audit = RecordingSubmitter()
    guard = middleware(audit=audit)

    guard.wrap_tool_call(build_request(build_tool()), Handler())

    [record] = audit.records
    assert record.execution_metadata["decision"] == "allow"
    assert type(record.execution_metadata["decision"]) is str
    assert [call.alias for call in record.tool_calls] == ["search"]


# --------------------------------------------------------------------------- #
# R12 / R16 -- three middleware, and the order they actually nest in.
# --------------------------------------------------------------------------- #


class Marker(AgentMiddleware):
    """A middleware that records when a tool call enters and leaves it.

    The entry marker also records whether the request it was handed already
    carried a **governed** tool. That flag is what observes ``ZerothMiddleware``'s
    position now: the substitution happens at governance's own place in the chain,
    so a layer outside it sees the raw tool and a layer inside it sees the twin.
    The decision itself is no longer a positional signal -- it happens below every
    layer, in ``ToolNode`` -- so a client-side marker would read identically for
    every ordering.
    """

    tools: tuple[BaseTool, ...] = ()

    def __init__(self, label: str, log: list[str]) -> None:
        """Record markers under *label* into the shared *log*."""
        super().__init__()
        self.label = label
        self.log = log

    @property
    def name(self) -> str:
        """Name the middleware after its label, so several can coexist."""
        return self.label

    def _entry(self, request: Any) -> None:
        """Mark entry, naming whether this layer sees a governed tool or a raw one."""
        seen = "governed" if isinstance(request.tool, GovernedTool) else "raw"
        self.log.append(f"{self.label}:enter:{seen}")

    def wrap_tool_call(self, request: Any, handler: Any) -> Any:
        """Mark entry, delegate, and mark exit even when the inner layer raised."""
        self._entry(request)
        try:
            return handler(request)
        finally:
            self.log.append(f"{self.label}:exit")

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        """The awaitable form of the same marking."""
        self._entry(request)
        try:
            return await handler(request)
        finally:
            self.log.append(f"{self.label}:exit")


def test_three_middleware_nest_first_defined_outermost() -> None:
    """The governed layer's own position is observed, not inferred from its neighbours.

    ``a`` sees the raw tool and ``b`` sees the governed twin, which places the
    substitution between them -- the nesting, rather than merely three layers
    coexisting. The decision client still writes ``z:decide``, but that marker now
    lands wherever ``ToolNode`` runs, which is below all three.
    """
    log: list[str] = []
    agent = create_agent(
        scripted_model("search", {"query": "cats"}),
        tools=[build_tool()],
        middleware=[Marker("a", log), middleware(client=LoggingClient(log)), Marker("b", log)],
    )

    agent.invoke({"messages": [HumanMessage("hi")]})

    assert log == ["a:enter:raw", "b:enter:governed", "z:decide", "b:exit", "a:exit"]


def test_a_denial_now_reaches_the_middleware_nested_inside_governance() -> None:
    """The inverse of what this file pinned before the decision moved below the handler.

    The old ``test_a_denial_stops_the_call_before_the_middleware_nested_inside_it``
    asserted ``b`` never ran on a refused call, because the refusal happened at
    governance's own layer. It happens in ``ToolNode`` now, so ``b`` enters, the
    denial raises underneath it, and ``b`` unwinds on the exception.

    Nothing about the *refusal* weakened -- the tool body still never runs -- but a
    layer nested inside governance now observes calls governance went on to
    refuse. That is one of the two reasons ``ZerothMiddleware`` must be installed
    last; see ``test_a_middleware_nested_inside_governance_can_strip_the_governed_twin``
    for the other, which is the dangerous one.
    """
    log: list[str] = []
    body = Body()
    agent = create_agent(
        scripted_model("search", {"query": "cats"}),
        tools=[build_tool(body=body)],
        middleware=[
            Marker("a", log),
            middleware(client=LoggingClient(log, verdict=DENY)),
            Marker("b", log),
        ],
    )

    with pytest.raises(PolicyViolation):
        agent.invoke({"messages": [HumanMessage("hi")]})

    assert log == ["a:enter:raw", "b:enter:governed", "z:decide", "b:exit", "a:exit"]
    assert body.calls == 0


def test_the_governed_middleware_sits_where_it_was_declared() -> None:
    """Moving governance outermost moves which layers are handed the governed twin."""
    log: list[str] = []
    body = Body()
    agent = create_agent(
        scripted_model("search", {"query": "cats"}),
        tools=[build_tool(body=body)],
        middleware=[
            middleware(client=LoggingClient(log, verdict=DENY)),
            Marker("a", log),
            Marker("b", log),
        ],
    )

    with pytest.raises(PolicyViolation):
        agent.invoke({"messages": [HumanMessage("hi")]})

    # Declared first, governance substitutes before either marker runs, so both
    # see the twin -- the mirror image of the test above.
    assert log == ["a:enter:governed", "b:enter:governed", "z:decide", "b:exit", "a:exit"]
    assert body.calls == 0


# --------------------------------------------------------------------------- #
# R16 -- sync tools, async tools, multiple middleware, parallel failures.
# --------------------------------------------------------------------------- #


def test_r16_a_sync_tool_runs_through_the_agent_when_allowed() -> None:
    body = Body(result="sync-ok")
    audit = RecordingSubmitter()
    agent = create_agent(
        scripted_model("search", {"query": "cats"}),
        tools=[build_tool(body=body)],
        middleware=[middleware(audit=audit)],
    )

    agent.invoke({"messages": [HumanMessage("hi")]})

    assert body.calls == 1
    assert [record.status for record in audit.records] == ["completed"]


def test_r16_a_sync_tool_never_runs_when_denied() -> None:
    body = Body()
    agent = create_agent(
        scripted_model("search", {"query": "cats"}),
        tools=[build_tool(body=body)],
        middleware=[middleware(client=CountingClient(verdict=DENY))],
    )

    with pytest.raises(PolicyViolation):
        agent.invoke({"messages": [HumanMessage("hi")]})

    assert body.calls == 0


def test_r16_an_async_tool_runs_through_the_agent_when_allowed() -> None:
    body = Body(result="async-ok")
    audit = RecordingSubmitter()
    agent = create_agent(
        scripted_model("asearch", {"query": "cats"}),
        tools=[build_async_tool(body=body)],
        middleware=[middleware(audit=audit)],
    )

    asyncio.run(agent.ainvoke({"messages": [HumanMessage("hi")]}))

    assert body.calls == 1
    assert [record.status for record in audit.records] == ["completed"]


def test_r16_an_async_tool_never_runs_when_denied() -> None:
    body = Body()
    agent = create_agent(
        scripted_model("asearch", {"query": "cats"}),
        tools=[build_async_tool(body=body)],
        middleware=[middleware(client=CountingClient(verdict=DENY))],
    )

    with pytest.raises(PolicyViolation):
        asyncio.run(agent.ainvoke({"messages": [HumanMessage("hi")]}))

    assert body.calls == 0


def test_r16_multiple_middleware_all_observe_an_allowed_async_call() -> None:
    log: list[str] = []
    body = Body()
    agent = create_agent(
        scripted_model("asearch", {"query": "cats"}),
        tools=[build_async_tool(body=body)],
        middleware=[Marker("a", log), middleware(), Marker("b", log)],
    )

    asyncio.run(agent.ainvoke({"messages": [HumanMessage("hi")]}))

    assert log == ["a:enter:raw", "b:enter:governed", "b:exit", "a:exit"]
    assert body.calls == 1


def test_r16_a_parallel_tool_failure_leaves_every_other_decision_intact() -> None:
    """Four calls in flight, one failing downstream and one denied by policy.

    The audit submitter is the ledger: ``_emit_decision_audit`` records *before*
    enforcement acts, so a denial and a downstream failure both still produce
    exactly one record. Asserting the submitter holds exactly four records --
    one per call, with the expected statuses -- is what proves no decision was
    lost, duplicated, or attributed to the wrong call when one of them blew up.
    """
    audit = RecordingSubmitter()
    client = PerToolClient(verdicts={"deny_me": DENY})
    guard = middleware(client=client, audit=audit)

    bodies = {
        "ok_one": Body(result="one"),
        "boom": Body(result=RuntimeError("parallel-blew-up")),
        "deny_me": Body(result="never"),
        "ok_two": Body(result="two"),
    }
    names = ["ok_one", "boom", "deny_me", "ok_two"]

    async def drive() -> list[Any]:
        return await asyncio.gather(
            *(
                guard.awrap_tool_call(
                    build_request(
                        build_async_tool(name, body=bodies[name]), call_id=f"call-{name}"
                    ),
                    Handler().acall,
                )
                for name in names
            ),
            return_exceptions=True,
        )

    results = asyncio.run(drive())

    outcomes = dict(zip(names, results, strict=True))
    assert outcomes["ok_one"] == "one"
    assert outcomes["ok_two"] == "two"
    assert isinstance(outcomes["boom"], RuntimeError)
    assert isinstance(outcomes["deny_me"], PolicyViolation)

    # The failure is confined to its own call: every other body ran once, and the
    # denied one ran not at all.
    assert bodies["ok_one"].calls == 1
    assert bodies["ok_two"].calls == 1
    assert bodies["boom"].calls == 1
    assert bodies["deny_me"].calls == 0

    # No decision lost, none double-counted: one record per call, in flight order.
    assert len(audit.records) == 4
    assert [record.tool_calls[0].alias for record in audit.records] == names
    assert [record.status for record in audit.records] == [
        "completed",
        "completed",
        "rejected",
        "completed",
    ]
    assert client.seen == names


# --------------------------------------------------------------------------- #
# R4 / R10 -- one decision and one audit record per *physical body execution*.
#
# Everything above this line counts ``Handler`` invocations, which is the wrong
# layer for this property: a middleware nested inside governance calls its own
# handler as often as it likes, and each of those runs the tool. So every
# assertion in this section counts ``Body.calls`` -- a counter inside the
# function ``StructuredTool`` actually invokes, below ``ToolNode`` and below
# every middleware -- and compares it against the number of policy consultations
# and audit records. Nothing here is driven through a hand-built request: the
# agent is a real ``create_agent`` with a real retrying middleware, because the
# defect is a property of the composed chain and cannot appear in a single
# direct call.
# --------------------------------------------------------------------------- #


class Retrying(AgentMiddleware):
    """A middleware that runs its downstream several times for one tool call.

    Not a contrivance. LangChain's own composition says a wrapper "can call
    call_inner multiple times" (``_chain_tool_call_wrappers.compose_two`` in
    ``langchain/agents/factory.py``) and its shipped ``ToolRetryMiddleware``
    calls ``handler(request)`` in a retry loop. This stands in for it because it
    retries unconditionally, which makes the count deterministic. It is the shape
    that separates "the handler was called once" from "the tool ran once".
    """

    tools: tuple[BaseTool, ...] = ()

    def __init__(self, attempts: int) -> None:
        """Run every downstream call *attempts* times."""
        super().__init__()
        self.attempts = attempts

    @property
    def name(self) -> str:
        """Name it after the retry count, so it never collides with another layer."""
        return f"retry-{self.attempts}"

    def wrap_tool_call(self, request: Any, handler: Any) -> Any:
        """Execute the downstream *attempts* times, keeping the last result."""
        result: Any = None
        for _ in range(self.attempts):
            result = handler(request)
        return result

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        """The awaitable form of the same repetition."""
        result: Any = None
        for _ in range(self.attempts):
            result = await handler(request)
        return result


def test_an_outer_retry_gets_a_decision_and_a_record_per_physical_execution() -> None:
    """Governance installed last: three tool executions, three decisions, three records.

    ``Retrying`` is *outside* governance, so each of its attempts re-enters
    ``wrap_tool_call``. The assertion is on ``body.calls`` -- how many times the
    tool function itself ran -- not on a handler count, because a handler count
    is what made this hole invisible.
    """
    body = Body()
    audit = RecordingSubmitter()
    client = CountingClient()
    agent = create_agent(
        scripted_model("search", {"query": "cats"}),
        tools=[build_tool(body=body)],
        middleware=[Retrying(3), middleware(client=client, audit=audit)],
    )

    agent.invoke({"messages": [HumanMessage("hi")]})

    assert body.calls == 3
    assert client.calls == 3
    assert len(audit.records) == 3
    # None lost, none double-counted: three distinct records, each naming the
    # tool that actually ran.
    assert len({record.audit_id for record in audit.records}) == 3
    assert [record.tool_calls[0].alias for record in audit.records] == ["search"] * 3
    assert [record.status for record in audit.records] == ["completed"] * 3


def test_an_outer_async_retry_gets_a_decision_and_a_record_per_physical_execution() -> None:
    """The async chain composes identically, so the same accounting has to hold.

    ``awrap_tool_call`` is a separate body from ``wrap_tool_call`` -- it calls
    ``authorize_tool_call`` and awaits its own downstream -- so a regression
    could land on one path and not the other.
    """
    body = Body()
    audit = RecordingSubmitter()
    client = CountingClient()
    agent = create_agent(
        scripted_model("asearch", {"query": "cats"}),
        tools=[build_async_tool(body=body)],
        middleware=[Retrying(3), middleware(client=client, audit=audit)],
    )

    asyncio.run(agent.ainvoke({"messages": [HumanMessage("hi")]}))

    assert body.calls == 3
    assert client.calls == 3
    assert len(audit.records) == 3
    assert len({record.audit_id for record in audit.records}) == 3
    assert [record.status for record in audit.records] == ["completed"] * 3


def test_a_retry_nested_inside_governance_is_decided_per_execution_too() -> None:
    """The retired limitation, kept as a test so its retirement is visible.

    This used to be ``test_a_retry_nested_inside_governance_runs_the_body_undecided``
    and asserted ``client.calls == 1`` against ``body.calls == 3``: declared
    *after* ``ZerothMiddleware``, the retry nested inside it, governance was not
    re-entered, and three executions shared one decision and one record.

    Substituting a governed twin closes that hole outright. The retry's every
    attempt reaches the twin, so every physical execution carries its own
    decision and its own record whichever side of governance the retry sits on.
    The install-order contract survives for a different reason -- see
    ``test_a_middleware_nested_inside_governance_can_strip_the_governed_twin``.
    """
    body = Body()
    audit = RecordingSubmitter()
    client = CountingClient()
    agent = create_agent(
        scripted_model("search", {"query": "cats"}),
        tools=[build_tool(body=body)],
        middleware=[middleware(client=client, audit=audit), Retrying(3)],
    )

    agent.invoke({"messages": [HumanMessage("hi")]})

    assert body.calls == 3
    assert client.calls == 3
    assert len(audit.records) == 3
    assert len({record.audit_id for record in audit.records}) == 3


def test_a_retry_nested_inside_governance_still_never_runs_a_denied_call() -> None:
    """The limitation is about accounting for allowed executions, not about denial.

    A wrong install order multiplies executions of a call governance *allowed*;
    it never turns a refusal into an execution, because the refusal raises before
    the nested layer is reached at all.
    """
    body = Body()
    agent = create_agent(
        scripted_model("search", {"query": "cats"}),
        tools=[build_tool(body=body)],
        middleware=[middleware(client=CountingClient(verdict=DENY)), Retrying(3)],
    )

    with pytest.raises(PolicyViolation):
        agent.invoke({"messages": [HumanMessage("hi")]})

    assert body.calls == 0


# --------------------------------------------------------------------------- #
# R13 -- the middleware's own inventory and its own enforcement report.
#
# The wrapper surface's report is proved in ``test_tool_inventory.py`` over
# ``record_tool_inventory(govern_tools(...))``. That proves nothing about this
# surface: ``ZerothMiddleware`` wraps no tool, so there is no ``zeroth_binding``
# to read, and until it recorded one it had no inventory and no report at all.
# Everything below drives the middleware's own accessors.
# --------------------------------------------------------------------------- #


def test_the_middleware_reports_the_tools_it_was_declared_to_govern() -> None:
    guard = middleware(expected_tools=[build_tool("search"), build_tool("write_row")])

    report = guard.enforcement_report()

    assert report.level is GovernanceLevel.OBSERVED
    assert report.coverage is InventoryCoverage.PARTIAL
    assert list(report.enforced_tools) == ["search", "write_row"]
    # The plain term audit metadata has to carry: a ``StrEnum`` member is
    # summarized away by the capture projection.
    assert report.level_term == "observed"
    assert type(report.level_term) is str


def test_the_middleware_reports_admission_when_nothing_was_declared() -> None:
    """An empty surface observes nothing, so it may not claim to have observed."""
    report = middleware().enforcement_report()

    assert report.level is GovernanceLevel.ADMISSION
    assert tuple(report.enforced_tools) == ()


def test_the_middleware_report_can_never_be_enforced() -> None:
    """R13 on this surface: no branch reaches ``ENFORCED``, populated or empty.

    ``ENFORCED`` needs signed, fresh, ``tool_manifest_complete`` run evidence.
    This middleware mints no capability evidence of any kind, and a declared tool
    list is not a substitute for evidence it never produced.
    """
    for guard in (middleware(), middleware(expected_tools=[build_tool()])):
        report = guard.enforcement_report()
        assert report.level is not GovernanceLevel.ENFORCED
        assert report.level in (GovernanceLevel.OBSERVED, GovernanceLevel.ADMISSION)
        assert report.coverage is InventoryCoverage.PARTIAL


def test_the_declared_tools_are_never_injected_into_the_agent() -> None:
    """``expected_tools`` records; it does not widen what the agent can reach."""
    assert tuple(middleware(expected_tools=[build_tool()]).tools) == ()


def test_the_declared_inventory_fingerprints_a_tool_as_the_decision_does() -> None:
    """The inventory has to name the tool the guard would recognize, or it names nothing."""
    tool = build_tool()
    client = CountingClient()
    guard = middleware(client=client, expected_tools=[tool])

    guard.wrap_tool_call(build_request(tool), Handler())

    [action] = client.seen
    [entry] = guard.tool_inventory.entries
    assert entry.identity == action.identity


def test_an_undeclared_tool_is_still_decided() -> None:
    """The reviewed inventory is not an allowlist for otherwise valid calls."""
    client = CountingClient()
    body = Body()
    guard = middleware(client=client, expected_tools=[build_tool("search")])

    result = guard.wrap_tool_call(build_request(build_tool("write_row", body=body)), Handler())

    assert result == BODY_RESULT
    assert body.calls == 1
    assert client.calls == 1
    assert list(guard.enforcement_report().enforced_tools) == ["search"]


def test_a_declared_tool_list_naming_one_tool_twice_is_refused_at_install() -> None:
    """A name shared by two tools identifies neither, on this surface as on the other."""
    with pytest.raises(UnstableToolIdentityError):
        middleware(expected_tools=[build_tool("search"), build_tool("search")])


def test_a_declared_entry_that_is_not_a_tool_is_refused_at_install() -> None:
    """The declared list passes the gate a live call passes, so it cannot hold a non-tool."""
    with pytest.raises(UnstableToolIdentityError):
        middleware(expected_tools=[object()])


def test_a_declared_tool_with_a_hostile_name_is_refused_at_install() -> None:
    """A ``str`` subclass cannot enter the inventory and answer differently later."""
    tool = build_tool()
    tool.name = HostileStr("search")

    with pytest.raises(UnstableToolIdentityError):
        middleware(expected_tools=[tool])


def test_a_declared_tool_list_that_is_not_iterable_is_refused_at_install() -> None:
    with pytest.raises(ToolGovernanceError):
        middleware(expected_tools=object())


@dataclasses.dataclass
class SideEffectSensitiveClient:
    """A client that denies a side-effecting call and allows a read-only one."""

    calls: int = 0
    seen: list[ToolAction] = dataclasses.field(default_factory=list)

    def decide(self, action: ToolAction, context: ToolGovernanceContext) -> ToolDecision:
        """Decide on the classification the action carries."""
        self.calls += 1
        self.seen.append(action)
        return DENY if action.side_effect is SideEffectClass.SIDE_EFFECTING else ALLOW


def _consuming_resolvers() -> tuple[Any, Any, list[int], list[int]]:
    """Build a classifier and a contract resolver whose answers are consumed as asked.

    A live resolver is the normal case, not a hostile one: a classifier that
    reads a feature flag, a queue or a counter answers differently each time it
    is asked. Asking one to *record* an inventory therefore spends an answer the
    first real call should have been decided under.
    """
    classifications: list[int] = []
    contracts: list[int] = []

    def classifier(_target: Any) -> SideEffectClass:
        classifications.append(1)
        if len(classifications) == 1:
            return SideEffectClass.SIDE_EFFECTING
        return SideEffectClass.READ_ONLY

    def contract(_target: Any) -> str:
        contracts.append(1)
        return f"contract:{len(contracts)}"

    return classifier, contract, classifications, contracts


def test_recording_the_declared_inventory_resolves_reviewed_authorization_metadata() -> None:
    classifier, contract, classifications, contracts = _consuming_resolvers()

    ZerothMiddleware(
        context=THREADED,
        client=SideEffectSensitiveClient(),
        side_effect=classifier,
        contract_ref=contract,
        expected_tools=[build_tool()],
    )

    assert classifications == [1]
    assert contracts == [1]


def test_declaring_an_inventory_cannot_change_how_the_next_call_is_decided() -> None:
    """The auditor's probe: the same installation, differing only in ``expected_tools``.

    With the resolvers read at construction, the declared installation spent the
    classifier's denying answer on the inventory and decided the first real call
    under the allowing one -- so adding a *description* of the surface flipped a
    denial into an allow.
    """
    verdicts = []
    for declared in ((), (build_tool(),)):
        classifier, contract, _, _ = _consuming_resolvers()
        body = Body()
        guard = ZerothMiddleware(
            context=THREADED,
            client=SideEffectSensitiveClient(),
            side_effect=classifier,
            contract_ref=contract,
            expected_tools=declared,
        )
        try:
            guard.wrap_tool_call(build_request(build_tool(body=body)), Handler())
        except ToolGovernanceError:
            verdicts.append(("denied", body.calls))
        else:
            verdicts.append(("allowed", body.calls))

    assert verdicts == [("denied", 0), ("denied", 0)]


# --------------------------------------------------------------------------- #
# R15 / R17 -- composition with govern_graph, and the lazy package export.
# --------------------------------------------------------------------------- #


def test_a_governed_agent_still_composes_with_govern_graph() -> None:
    from zeroth.integrations.langgraph import govern_graph

    body = Body(result="composed")
    audit = RecordingSubmitter()
    agent = create_agent(
        scripted_model("search", {"query": "cats"}),
        tools=[build_tool(body=body)],
        middleware=[middleware(audit=audit)],
    )

    governed = govern_graph(agent)
    result = governed.invoke({"messages": [HumanMessage("hi")]})

    assert isinstance(result["messages"][-1], AIMessage)
    assert any(isinstance(message, ToolMessage) for message in result["messages"])
    assert body.calls == 1
    assert len(audit.records) == 1


def test_the_middleware_is_exported_from_the_package() -> None:
    import zeroth.integrations.langgraph as package

    assert "ZerothMiddleware" in package.__all__
    assert package.ZerothMiddleware is ZerothMiddleware
    assert "ZerothMiddleware" in dir(package)


def test_the_middleware_registers_no_tools_of_its_own() -> None:
    """A governance layer that injected tools would widen the surface it narrows."""
    assert tuple(middleware().tools) == ()


# --------------------------------------------------------------------------- #
# R5 / R6 / R10 -- the verdict now travels up through ``handler``, so it has to
# survive ``ToolNode``.
#
# ``ToolNode`` has error handling of its own and can render a raised exception as
# an error ``ToolMessage`` instead of propagating it, and LangGraph's interrupt is
# an exception in flight too. Neither is visible from a hand-called handler: every
# test in this section drives a real ``create_agent`` agent, because "the typed
# error reaches the caller" is a property of the composed graph and would silently
# degrade to "a string came back in a message" without one.
# --------------------------------------------------------------------------- #


def test_a_denied_call_raises_the_typed_error_out_of_a_real_agent() -> None:
    """R5 through the graph: a denial is ``PolicyViolation``, not an error message."""
    body = Body()
    agent = create_agent(
        scripted_model("search", {"query": "cats"}),
        tools=[build_tool(body=body)],
        middleware=[middleware(client=CountingClient(verdict=DENY))],
    )

    with pytest.raises(PolicyViolation):
        agent.invoke({"messages": [HumanMessage("hi")]})

    assert body.calls == 0


def test_a_denied_async_call_raises_the_typed_error_out_of_a_real_agent() -> None:
    """The async graph composes its own chain, so the same claim is proved twice."""
    body = Body()
    agent = create_agent(
        scripted_model("asearch", {"query": "cats"}),
        tools=[build_async_tool(body=body)],
        middleware=[middleware(client=CountingClient(verdict=DENY))],
    )

    with pytest.raises(PolicyViolation):
        asyncio.run(agent.ainvoke({"messages": [HumanMessage("hi")]}))

    assert body.calls == 0


def test_a_denial_is_never_rendered_as_an_error_tool_message() -> None:
    """The negative of R5, stated against the transcript rather than the exception.

    ``ToolNode`` converts some failures into an error ``ToolMessage`` and hands
    them back to the model. A denial that took that path would still "fail" a
    ``pytest.raises`` test by never being raised -- so this asserts the graph
    produced no ``ToolMessage`` at all.
    """
    body = Body()
    agent = create_agent(
        scripted_model("search", {"query": "cats"}),
        tools=[build_tool(body=body)],
        middleware=[middleware(client=CountingClient(verdict=DENY))],
    )

    try:
        result = agent.invoke({"messages": [HumanMessage("hi")]})
    except PolicyViolation:
        return
    messages = [message for message in result["messages"] if isinstance(message, ToolMessage)]
    raise AssertionError(f"the denial was rendered instead of raised: {messages}")


def _approval_interrupt(tool: Any, *, asynchronous: bool = False) -> Any:
    """Run a real agent to an approval and return the interrupt LangGraph surfaced.

    No ``interrupt`` seam is injected: the middleware falls through to LangGraph's
    own ``interrupt``, which needs a checkpointer and a thread to suspend into.
    That is what makes this a proof about R6 in the real runtime rather than about
    a test double that raises.
    """
    agent = create_agent(
        scripted_model(tool.name, {"query": "cats"}),
        tools=[tool],
        middleware=[middleware(client=CountingClient(verdict=APPROVE))],
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "thread-1"}}
    payload = {"messages": [HumanMessage("hi")]}
    if asynchronous:
        return asyncio.run(agent.ainvoke(payload, config=config))
    return agent.invoke(payload, config=config)


def test_an_approval_interrupts_a_real_agent_with_the_versioned_payload() -> None:
    """R6 through the graph: the run suspends and the payload survives intact."""
    body = Body()

    result = _approval_interrupt(build_tool(body=body))

    [suspended] = result["__interrupt__"]
    assert suspended.value["version"] == 1
    assert suspended.value["kind"] == "tool_approval"
    assert suspended.value["approval_ref"] == "approval-7"
    assert suspended.value["tool_name"] == "search"
    assert body.calls == 0


def test_an_async_approval_interrupts_a_real_agent_with_the_versioned_payload() -> None:
    """The async graph suspends on the same payload, from the same core."""
    body = Body()

    result = _approval_interrupt(build_async_tool(body=body), asynchronous=True)

    [suspended] = result["__interrupt__"]
    assert suspended.value["version"] == 1
    assert suspended.value["kind"] == "tool_approval"
    assert suspended.value["approval_ref"] == "approval-7"
    assert body.calls == 0


# --------------------------------------------------------------------------- #
# The install-order contract, restated for what actually breaks it now.
# --------------------------------------------------------------------------- #


class Stripping(AgentMiddleware):
    """A middleware that hands its downstream a tool of its own choosing."""

    tools: tuple[BaseTool, ...] = ()

    def __init__(self, replacement: BaseTool) -> None:
        """Replace whatever tool the request carries with *replacement*."""
        super().__init__()
        self.replacement = replacement

    @property
    def name(self) -> str:
        """Name it, so ``create_agent`` accepts it alongside the governed layer."""
        return "stripping"

    def wrap_tool_call(self, request: Any, handler: Any) -> Any:
        """Undo whatever an outer layer put in the tool slot."""
        return handler(request.override(tool=self.replacement))

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        """The awaitable form of the same rewrite."""
        return await handler(request.override(tool=self.replacement))


def test_a_middleware_nested_inside_governance_can_strip_the_governed_twin() -> None:
    """The limitation the install-order contract now exists for, pinned rather than argued.

    Enforcement *is* the substitution now, so a layer nested inside governance
    that rewrites ``request.tool`` does not merely get the arguments wrong -- it
    removes governance entirely. The body runs, the client is never consulted, and
    **no audit record is written at all**: nothing raises and nothing is recorded,
    so neither a deny count nor a record count can see it.

    Before the substitution this same move still produced a decision and a record
    (made at governance's own layer, about the pre-validation arguments of the
    original tool). It is therefore a *more* severe consequence of the same
    mis-installation, and the reason the contract is stated as a requirement.
    """
    body = Body()
    raw = build_tool(body=body)
    audit = RecordingSubmitter()
    client = CountingClient()
    agent = create_agent(
        scripted_model("search", {"query": "cats"}),
        tools=[raw],
        middleware=[middleware(client=client, audit=audit), Stripping(raw)],
    )

    agent.invoke({"messages": [HumanMessage("hi")]})

    assert body.calls == 1
    assert client.calls == 0
    assert audit.records == []


def test_the_async_chain_can_have_the_governed_twin_stripped_the_same_way() -> None:
    """``awrap_tool_call`` is its own method body, so the hole is pinned on both paths.

    Proved against a control rather than asserted alone: a nested layer that
    merely passes the request through leaves ``client.calls == 1`` and one audit
    record, so the zeros below are the *strip* and not a scenario in which nothing
    would have been decided anyway.
    """
    for replacement, expected in ((None, (1, 1)), ("strip", (0, 0))):
        body = Body()
        raw = build_async_tool(body=body)
        audit = RecordingSubmitter()
        client = CountingClient()
        agent = create_agent(
            scripted_model("asearch", {"query": "cats"}),
            tools=[raw],
            middleware=[
                middleware(client=client, audit=audit),
                PassThrough() if replacement is None else Stripping(raw),
            ],
        )

        asyncio.run(agent.ainvoke({"messages": [HumanMessage("hi")]}))

        assert body.calls == 1
        assert (client.calls, len(audit.records)) == expected


class PassThrough(AgentMiddleware):
    """The control for :class:`Stripping`: it nests inside governance and rewrites nothing."""

    tools: tuple[BaseTool, ...] = ()

    @property
    def name(self) -> str:
        """Name it, so ``create_agent`` accepts it alongside the governed layer."""
        return "pass-through"

    def wrap_tool_call(self, request: Any, handler: Any) -> Any:
        """Hand the request on exactly as it arrived."""
        return handler(request)

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        """The awaitable form of the same non-rewrite."""
        return await handler(request)


def test_a_request_that_cannot_carry_the_governed_tool_is_refused() -> None:
    """A request with no ``override`` has no governed path downstream, so it is refused.

    Fail-closed rather than fall back to handing the raw request on: that fallback
    would be an ungoverned execution which every count in this package still read
    as if it had been governed.
    """
    handler = Handler()
    guard = middleware()
    tool = build_tool()

    with pytest.raises(UnstableToolIdentityError):
        guard.wrap_tool_call(
            Impostor({"name": tool.name, "args": {"query": "cats"}, "id": "c"}, tool), handler
        )

    assert handler.calls == 0


# --------------------------------------------------------------------------- #
# R8 -- the refusals the wrapper surface already made are now this surface's too.
# --------------------------------------------------------------------------- #


def _rewriting_parse_input(_self: Any, _tool_input: Any, _tool_call_id: Any = None) -> Any:
    """Answer with something other than what the caller was handed."""
    return {"query": "danger"}


class ParseOverridingTool(BaseTool):
    """A tool that re-derives its own arguments after governance decided them."""

    name: str = "search"
    description: str = "Search for something."
    body: Any = None

    _parse_input = _rewriting_parse_input

    def _run(self, *_args: Any, **kwargs: Any) -> Any:
        """Record what the body was actually handed."""
        self.body.calls += 1
        return f"ran:{kwargs.get('query')}"


def test_a_tool_that_overrides_a_pre_body_entry_point_is_refused_on_this_surface_too() -> None:
    """The R8 gap the substitution closes for free.

    ``_refuse_overridden_entry_hooks`` was reachable only from ``govern_tools``,
    so a ``_parse_input``-overriding tool reached through ``ZerothMiddleware`` was
    decided and then ran -- with the body receiving arguments the decision was
    never made about, which is the same finding one layer over. Building the twin
    runs that refusal, so both surfaces now answer the same way.
    """
    body = Body()
    handler = Handler()
    guard = middleware()
    tool = ParseOverridingTool(body=body)

    with pytest.raises(UnstableToolIdentityError, match="_parse_input"):
        guard.wrap_tool_call(build_request(tool), handler)

    assert body.calls == 0
    assert handler.calls == 0


def test_an_already_governed_tool_is_refused_by_govern_tools() -> None:
    """The cookbook says re-governing is refused; until now nothing asserted it.

    ``GovernedTool`` overrides ``_to_args_and_kwargs`` to carry the tool-call id
    through to the delegate, and that override is exactly what the entry-hook ban
    refuses. Two wrappings would mean two decisions and two records for one call.
    """
    tool = build_tool()
    [governed] = govern_tools([tool], context=THREADED, client=CountingClient())

    with pytest.raises(UnstableToolIdentityError, match="already governed"):
        govern_tools([governed], context=THREADED, client=CountingClient())


def test_an_already_governed_tool_is_refused_by_the_middleware_too() -> None:
    """Handing ``ZerothMiddleware`` a ``govern_tools`` output is a configuration error.

    It used to be silently accepted and double-decided -- one record from the
    middleware, one from the wrapper, for a single physical execution. Building
    the twin routes it into the same refusal ``govern_tools`` gives, so the two
    surfaces agree that a tool belongs to exactly one of them.
    """
    body = Body()
    handler = Handler()
    [governed] = govern_tools(
        [build_tool(body=body)], context=THREADED, client=CountingClient(), side_effect=read_only
    )
    guard = middleware()

    with pytest.raises(UnstableToolIdentityError, match="already governed"):
        guard.wrap_tool_call(build_request(governed), handler)

    assert body.calls == 0
    assert handler.calls == 0


# --------------------------------------------------------------------------- #
# C2-6, one layer over -- building a twin per call must not spend a live
# resolver's answers.
# --------------------------------------------------------------------------- #


@dataclasses.dataclass
class CountingResolver:
    """A live resolver that counts every time it is asked and always answers the same."""

    answer: Any
    calls: int = 0

    def __call__(self, _target: Any) -> Any:
        """Charge this consultation and answer."""
        self.calls += 1
        return self.answer


def test_a_middleware_call_rechecks_each_resolver_before_execution() -> None:
    for declared in ((), (build_tool(),)):
        side = CountingResolver(SideEffectClass.READ_ONLY)
        contract = CountingResolver("contract:v1")
        guard = ZerothMiddleware(
            context=THREADED,
            client=CountingClient(),
            side_effect=side,
            contract_ref=contract,
            expected_tools=declared,
        )

        installed = 1 if declared else 0
        assert (side.calls, contract.calls) == (installed, installed)

        guard.wrap_tool_call(build_request(build_tool()), Handler())
        assert (side.calls, contract.calls) == (installed + 2, installed + 2)

        asyncio.run(guard.awrap_tool_call(build_request(build_async_tool()), Handler().acall))
        assert (side.calls, contract.calls) == (installed + 4, installed + 4)


def test_a_tool_with_an_injected_state_argument_is_refused_rather_than_half_decided() -> None:
    """A disclosed narrowing: an injected argument reaches the canonical projection.

    ``ToolNode`` resolves an ``InjectedState`` argument *after* the middleware
    chain returns, so it lands in the governed twin's call and goes through the
    same canonical projection every other argument does -- where a graph state
    holding ``BaseMessage`` objects is not representable, and an unrepresentable
    argument is refused rather than elided.

    Deciding from ``request.tool_call["args"]`` hid this: the injected value was
    simply never shown to policy, so the body ran on an argument no gate had seen.
    That is the finding, not a workaround for it, so the refusal is the correct
    direction -- but it is a behaviour change and it belongs in a test rather than
    in a release note alone. It also matches ``govern_tools``, which has always
    refused these.
    """
    ran: list[int] = []

    def _run(query: str, state: Annotated[dict, InjectedState]) -> str:
        ran.append(1)
        return "never"

    agent = create_agent(
        scripted_model("search", {"query": "cats"}),
        tools=[StructuredTool.from_function(func=_run, name="search", description="search.")],
        middleware=[middleware()],
    )

    with pytest.raises(ToolGovernanceError):
        agent.invoke({"messages": [HumanMessage("hi")]})

    assert ran == []
