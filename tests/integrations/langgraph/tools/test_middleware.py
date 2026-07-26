"""Proof that ``ZerothMiddleware`` governs a ``create_agent`` agent's tool calls.

**"Denied" is "the downstream ran zero times", and here that is directly
observable.** LangChain hands the tool's execution in as a ``handler`` callback,
so every enforcement assertion below counts handler invocations: a denial is
``pytest.raises`` *and* ``handler.calls == 0``, an allow is ``== 1``, never merely
truthy. A middleware that invoked first and raised afterwards satisfies every
``pytest.raises`` in a suite that does not count.

**Two different layers are counted here, and the distinction is load-bearing.**
A handler count answers "did this middleware call its downstream", which is the
right measurement for allow/deny/approval. It is the *wrong* measurement for how
many times the tool ran: a middleware nested inside governance calls its own
handler as often as it likes, so one handler call can be any number of
executions. The sections marked as counting ``Body.calls`` count a counter inside
the function ``StructuredTool`` invokes -- below ``ToolNode``, below every
middleware -- and that is the only number that says how often the tool body
physically ran.

**Nesting order is asserted, not assumed (R12).** Three middleware each append an
entry *and* an exit marker, and the test pins the exact six-element sequence.
Entry-only markers would observe dispatch, not nesting -- and nesting is the
property that decides whether an inner middleware can see a call governance
refused, and whether a retry gets its own decision.

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
from typing import Any

import pytest

pytest.importorskip("langchain.agents", reason="requires the gateway-conformance dependency group")

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, ToolCallRequest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool

from tests.integrations.langgraph.genai._causal import HostileStr
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
from zeroth.integrations.langgraph._tool_wrappers import govern_tools

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

    The governed middleware appends no marker of its own -- it is production code
    -- so its position in a middleware chain is observed here, at the one moment
    it does something the test can see: consulting policy.
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
    """The downstream LangChain would run, counting every time it is reached."""

    result: Any = "handler-result"
    calls: int = 0
    seen: list[Any] = dataclasses.field(default_factory=list)

    def __call__(self, request: Any) -> Any:
        """Count this invocation and return the configured result."""
        self.calls += 1
        self.seen.append(request)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def acall(self, request: Any) -> Any:
        """The awaitable form of the same counting downstream."""
        await asyncio.sleep(0)
        return self(request)


def read_only(_target: object) -> SideEffectClass:
    """Classify every tool as read-only, so the allow path needs no blanket opt-in."""
    return SideEffectClass.READ_ONLY


@dataclasses.dataclass
class Body:
    """A tool body that counts every execution."""

    result: str = "body-result"
    calls: int = 0

    def run(self, **_kwargs: Any) -> str:
        """Count this execution and return the configured result."""
        self.calls += 1
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
# R9 / R10 -- the handler is the downstream, and its call count is the evidence.
# --------------------------------------------------------------------------- #


def test_an_allowed_call_runs_the_handler_exactly_once() -> None:
    handler = Handler()
    client = CountingClient(verdict=ALLOW)
    guard = middleware(client=client)

    result = guard.wrap_tool_call(build_request(build_tool()), handler)

    assert result == "handler-result"
    assert handler.calls == 1
    assert client.calls == 1


def test_a_denied_call_runs_the_handler_zero_times() -> None:
    handler = Handler()
    guard = middleware(client=CountingClient(verdict=DENY))

    with pytest.raises(PolicyViolation):
        guard.wrap_tool_call(build_request(build_tool()), handler)

    assert handler.calls == 0


def test_an_allowed_async_call_awaits_the_handler_exactly_once() -> None:
    handler = Handler()
    guard = middleware()

    result = asyncio.run(guard.awrap_tool_call(build_request(build_tool()), handler.acall))

    assert result == "handler-result"
    assert handler.calls == 1


def test_a_denied_async_call_runs_the_handler_zero_times() -> None:
    handler = Handler()
    guard = middleware(client=CountingClient(verdict=DENY))

    with pytest.raises(PolicyViolation):
        asyncio.run(guard.awrap_tool_call(build_request(build_tool()), handler.acall))

    assert handler.calls == 0


def test_an_approval_suspends_before_the_handler_is_reached() -> None:
    handler = Handler()
    pause = RecordingInterrupt()
    guard = middleware(client=CountingClient(verdict=APPROVE), interrupt=pause)

    with pytest.raises(Suspended):
        guard.wrap_tool_call(build_request(build_tool()), handler)

    assert handler.calls == 0
    assert len(pause.payloads) == 1
    assert pause.payloads[0]["approval_ref"] == "approval-7"


def test_an_async_approval_suspends_before_the_handler_is_reached() -> None:
    handler = Handler()
    pause = RecordingInterrupt()
    guard = middleware(client=CountingClient(verdict=APPROVE), interrupt=pause)

    with pytest.raises(Suspended):
        asyncio.run(guard.awrap_tool_call(build_request(build_tool()), handler.acall))

    assert handler.calls == 0
    assert len(pause.payloads) == 1


def test_an_approval_without_a_thread_never_reaches_the_handler() -> None:
    handler = Handler()
    pause = RecordingInterrupt()
    guard = middleware(context=THREADLESS, client=CountingClient(verdict=APPROVE), interrupt=pause)

    with pytest.raises(ApprovalRequiresThreadError):
        guard.wrap_tool_call(build_request(build_tool()), handler)

    assert handler.calls == 0
    assert pause.payloads == []


def test_a_tool_error_propagates_unchanged_and_is_never_retried() -> None:
    handler = Handler(result=RuntimeError("tool-blew-up"))
    guard = middleware()

    with pytest.raises(RuntimeError, match="tool-blew-up"):
        guard.wrap_tool_call(build_request(build_tool()), handler)

    assert handler.calls == 1


# --------------------------------------------------------------------------- #
# Fail-closed request handling: nothing shaped like a request slips an allow.
# --------------------------------------------------------------------------- #


def test_a_call_with_no_governance_context_never_reaches_the_handler() -> None:
    handler = Handler()
    guard = ZerothMiddleware(client=CountingClient(), side_effect=read_only)

    with pytest.raises(GovernanceContextError):
        guard.wrap_tool_call(build_request(build_tool()), handler)

    assert handler.calls == 0


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


def test_unrepresentable_arguments_never_reach_the_handler() -> None:
    handler = Handler()
    guard = middleware()

    with pytest.raises(ToolGovernanceError):
        guard.wrap_tool_call(build_request(build_tool(), args={"query": object()}), handler)

    assert handler.calls == 0


def test_an_unclassified_tool_is_denied_by_default() -> None:
    """No classifier means unknown, and unknown is refused without consulting a client."""
    handler = Handler()
    guard = ZerothMiddleware(context=THREADED, client=CountingClient())

    with pytest.raises(PolicyViolation):
        guard.wrap_tool_call(build_request(build_tool()), handler)

    assert handler.calls == 0


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
    """A middleware that records when a tool call enters and leaves it."""

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

    def wrap_tool_call(self, request: Any, handler: Any) -> Any:
        """Mark entry, delegate, and mark exit even when the inner layer raised."""
        self.log.append(f"{self.label}:enter")
        try:
            return handler(request)
        finally:
            self.log.append(f"{self.label}:exit")

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        """The awaitable form of the same marking."""
        self.log.append(f"{self.label}:enter")
        try:
            return await handler(request)
        finally:
            self.log.append(f"{self.label}:exit")


class ToolCallingModel(GenericFakeChatModel):
    """A fake chat model that emits scripted tool calls and accepts a tool binding."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        """Accept the binding unchanged: the scripted messages already name the tool."""
        return self


def scripted_model(tool_name: str, arguments: dict[str, Any]) -> ToolCallingModel:
    """Build a model that asks for one tool call and then answers."""
    return ToolCallingModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[{"name": tool_name, "args": arguments, "id": "call-1"}],
                ),
                AIMessage(content="done"),
            ]
        )
    )


def test_three_middleware_nest_first_defined_outermost() -> None:
    """The governed layer's own position is observed, not inferred from its neighbours.

    ``a`` before ``b`` would hold whatever ``ZerothMiddleware`` did, so the
    decision client writes ``z:decide`` into the *same* log. Its position between
    ``a:enter`` and ``b:enter`` is what pins governance inside ``a`` and outside
    ``b`` -- the nesting, rather than merely three layers coexisting.
    """
    log: list[str] = []
    agent = create_agent(
        scripted_model("search", {"query": "cats"}),
        tools=[build_tool()],
        middleware=[Marker("a", log), middleware(client=LoggingClient(log)), Marker("b", log)],
    )

    agent.invoke({"messages": [HumanMessage("hi")]})

    assert log == ["a:enter", "z:decide", "b:enter", "b:exit", "a:exit"]


def test_a_denial_stops_the_call_before_the_middleware_nested_inside_it() -> None:
    """The order matters because it decides what an inner middleware ever sees."""
    log: list[str] = []
    agent = create_agent(
        scripted_model("search", {"query": "cats"}),
        tools=[build_tool()],
        middleware=[
            Marker("a", log),
            middleware(client=LoggingClient(log, verdict=DENY)),
            Marker("b", log),
        ],
    )

    with pytest.raises(PolicyViolation):
        agent.invoke({"messages": [HumanMessage("hi")]})

    assert log == ["a:enter", "z:decide", "a:exit"]


def test_the_governed_middleware_sits_where_it_was_declared() -> None:
    """Moving governance outermost moves what the other layers observe."""
    log: list[str] = []
    agent = create_agent(
        scripted_model("search", {"query": "cats"}),
        tools=[build_tool()],
        middleware=[
            middleware(client=LoggingClient(log, verdict=DENY)),
            Marker("a", log),
            Marker("b", log),
        ],
    )

    with pytest.raises(PolicyViolation):
        agent.invoke({"messages": [HumanMessage("hi")]})

    assert log == ["z:decide"]


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

    assert log == ["a:enter", "b:enter", "b:exit", "a:exit"]
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

    handlers = {
        "ok_one": Handler(result="one"),
        "boom": Handler(result=RuntimeError("parallel-blew-up")),
        "deny_me": Handler(result="never"),
        "ok_two": Handler(result="two"),
    }
    names = ["ok_one", "boom", "deny_me", "ok_two"]

    async def drive() -> list[Any]:
        return await asyncio.gather(
            *(
                guard.awrap_tool_call(
                    build_request(build_tool(name), call_id=f"call-{name}"),
                    handlers[name].acall,
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

    # The failure is confined to its own call: every other downstream ran once,
    # and the denied one ran not at all.
    assert handlers["ok_one"].calls == 1
    assert handlers["ok_two"].calls == 1
    assert handlers["boom"].calls == 1
    assert handlers["deny_me"].calls == 0

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


def test_a_retry_nested_inside_governance_runs_the_body_undecided() -> None:
    """The documented limitation, pinned so it can never quietly become a claim.

    Declared *after* ``ZerothMiddleware``, the retry nests inside it and calls
    LangChain's executor directly. Governance is not re-entered, so three
    executions share one decision and one record. Nothing detects this from
    inside a middleware -- see ``_middleware.py``'s module docstring -- which is
    why ``ZerothMiddleware`` must be installed last, and why this test asserts
    the real numbers rather than the ones the ordering contract promises.
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
    assert client.calls == 1
    assert len(audit.records) == 1


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


def test_an_undeclared_tool_is_still_decided_and_the_inventory_gates_nothing() -> None:
    """The report is a description of the surface, never a second refusal on it."""
    client = CountingClient()
    handler = Handler()
    guard = middleware(client=client, expected_tools=[build_tool("search")])

    result = guard.wrap_tool_call(build_request(build_tool("write_row")), handler)

    assert result == "handler-result"
    assert handler.calls == 1
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
