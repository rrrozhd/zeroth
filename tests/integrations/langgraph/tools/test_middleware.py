"""Proof that ``ZerothMiddleware`` governs a ``create_agent`` agent's tool calls.

**"Denied" is "the downstream ran zero times", and here that is directly
observable.** LangChain hands the tool's execution in as a ``handler`` callback,
so every enforcement assertion below counts handler invocations: a denial is
``pytest.raises`` *and* ``handler.calls == 0``, an allow is ``== 1``, never merely
truthy. A middleware that invoked first and raised afterwards satisfies every
``pytest.raises`` in a suite that does not count.

**Nesting order is asserted, not assumed (R12).** Three middleware each append an
entry *and* an exit marker, and the test pins the exact six-element sequence.
Entry-only markers would observe dispatch, not nesting -- and nesting is the
property that decides whether an inner middleware can see a call governance
refused.

**Tier A.** This suite needs ``langchain.agents``, which ships only in the
optional ``gateway-conformance`` group, so it carries both guards: an
``importorskip`` before any langchain import and the ``langgraph_conformance``
marker. ``addopts`` deselects that marker, so these tests do **not** run in the
default suite (or in the pre-commit hook); run them with
``-o addopts= -m langgraph_conformance``.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import pytest

pytest.importorskip("langgraph", reason="requires the gateway-conformance dependency group")

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, ToolCallRequest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool

from tests.integrations.langgraph.genai._causal import HostileStr
from tests.integrations.langgraph.tools._hostile import HostileDict, HostileKey
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
