"""Issue-faithful LangGraph scenarios for Zeroth's owned safety boundaries."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import StructuredTool
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

from tests.integrations.langgraph.test_approval_lifecycle import PersistentSaver
from zeroth.governance.audit.models import NodeAuditRecord
from zeroth.governance.identity import ActorIdentity, AuthMethod
from zeroth.integrations.langgraph import (
    ActionExecutionState,
    ApprovalCoordinator,
    ApprovalDecision,
    ApprovalResolution,
    ApprovalState,
    DuplicateToolExecutionError,
    SideEffectClass,
    SQLiteActionExecutionRepository,
    SQLiteApprovalRepository,
    ToolDecision,
    ToolDecisionKind,
    ToolGovernanceContext,
    govern_graph,
    govern_tools,
)

CONTEXT = ToolGovernanceContext(
    tenant_id="tenant-a",
    principal_id="principal-1",
    run_id="run-issue-simulation",
    thread_id="thread-issue-simulation",
)
ALLOW = ToolDecision(ToolDecisionKind.ALLOW, "unknown_error")


@dataclass
class AllowClient:
    def decide(self, action: Any, context: Any) -> ToolDecision:
        del action, context
        return ALLOW


@dataclass
class ApprovalThenAllowClient:
    calls: int = 0

    def decide(self, action: Any, context: Any) -> ToolDecision:
        del action, context
        self.calls += 1
        if self.calls == 1:
            return ToolDecision(
                ToolDecisionKind.REQUIRE_APPROVAL,
                "policy_violation",
                approval_ref="approval-issue-8304",
            )
        return ALLOW


@dataclass
class RecordingAudit:
    records: list[NodeAuditRecord] = field(default_factory=list)

    def submit(self, record: NodeAuditRecord) -> None:
        self.records.append(record)


def _tool_input(name: str, arguments: dict[str, Any], call_id: str) -> dict[str, Any]:
    return {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[{"name": name, "args": arguments, "id": call_id}],
            )
        ]
    }


@pytest.mark.asyncio
async def test_7417_duplicate_delivery_applies_one_business_side_effect(tmp_path: Any) -> None:
    """A Cloud-style redispatch may enter ToolNode twice but the effect lands once."""
    path = tmp_path / "actions.sqlite3"
    repositories = (
        SQLiteActionExecutionRepository(path),
        SQLiteActionExecutionRepository(path),
    )
    started = asyncio.Event()
    release = asyncio.Event()
    effects: list[str] = []

    async def charge(order_id: str) -> dict[str, str]:
        effects.append(order_id)
        started.set()
        await release.wait()
        return {"receipt": f"charged:{order_id}"}

    tool = StructuredTool.from_function(
        coroutine=charge,
        name="charge",
        description="Charge one order.",
    )
    graphs = []
    for repository in repositories:
        [governed] = govern_tools(
            [tool],
            context=CONTEXT,
            client=AllowClient(),
            side_effect=lambda _tool: SideEffectClass.SIDE_EFFECTING,
            action_lifecycle=repository,
        )
        builder = StateGraph(MessagesState)
        builder.add_node("tools", ToolNode([governed]))
        builder.add_edge(START, "tools")
        builder.add_edge("tools", END)
        graphs.append(builder.compile())
    payload = _tool_input("charge", {"order_id": "order-41"}, "call-long-running-41")

    original = asyncio.create_task(graphs[0].ainvoke(payload))
    await asyncio.wait_for(started.wait(), timeout=2)
    with pytest.raises(DuplicateToolExecutionError, match="already in flight"):
        await graphs[1].ainvoke(payload)
    release.set()
    original_output = await asyncio.wait_for(original, timeout=2)
    replay_output = await graphs[1].ainvoke(payload)

    assert effects == ["order-41"]
    assert original_output["messages"][0].content == replay_output["messages"][0].content
    [record] = repositories[1].records()
    assert record.state is ActionExecutionState.COMPLETED
    assert record.tool_call_id == "call-long-running-41"


def test_8304_approval_evidence_binds_call_actor_decision_and_result(tmp_path: Any) -> None:
    """A real interrupt/resume retains the call id without out-of-band recovery."""
    approvals = SQLiteApprovalRepository(tmp_path / "approvals.sqlite3")
    saver = PersistentSaver(tmp_path / "checkpoints.bin")
    policy = ApprovalThenAllowClient()
    audit = RecordingAudit()
    actor = ActorIdentity(
        subject="principal-1",
        auth_method=AuthMethod.API_KEY,
        tenant_id="tenant-a",
    )
    effects: list[int] = []

    def delete_invoice(invoice_id: int) -> str:
        effects.append(invoice_id)
        return f"deleted:{invoice_id}"

    tool = StructuredTool.from_function(
        func=delete_invoice,
        name="delete_invoice",
        description="Delete one invoice.",
    )
    [governed] = govern_tools(
        [tool],
        context=CONTEXT,
        client=policy,
        side_effect=lambda _tool: SideEffectClass.SIDE_EFFECTING,
        approval_lifecycle=approvals,
        audit=audit,
        actor=actor,
    )
    builder = StateGraph(MessagesState)
    builder.add_node("tools", ToolNode([governed]))
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    graph = govern_graph(builder.compile(checkpointer=saver))
    config = {"configurable": {"thread_id": CONTEXT.thread_id}}

    graph.invoke(
        _tool_input("delete_invoice", {"invoice_id": 41}, "call-approval-41"),
        config,
    )
    interrupted = graph.get_state(config)
    [interrupt] = interrupted.interrupts
    payload = interrupt.value
    assert payload["tool_call_id"] == "call-approval-41"
    assert payload["run_id"] == CONTEXT.run_id
    assert payload["thread_id"] == CONTEXT.thread_id
    assert payload["tool_fingerprint"]
    assert payload["argument_fingerprint"]

    coordinator = ApprovalCoordinator(approvals)
    coordinator.confirm_checkpoint(
        "approval-issue-8304",
        graph,
        config=config,
        durable_checkpointer=saver,
    )
    approvals.decide(
        ApprovalResolution("approval-issue-8304", ApprovalDecision.APPROVE)
    )
    completed = coordinator.resume(
        "approval-issue-8304",
        graph,
        owner="approval-worker",
        config=config,
        durable_checkpointer=saver,
    )

    assert completed.state is ApprovalState.RESOLVED
    assert effects == [41]
    final = audit.records[-1]
    [call] = final.tool_calls
    assert final.actor == actor
    assert final.execution_metadata["decision"] == "approve"
    assert call.tool_call_id == "call-approval-41"
    expected = hashlib.sha256(
        json.dumps("deleted:41", separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    assert call.outcome == {"result_fingerprint": expected}
    state = graph.get_state(config)
    assert any(
        isinstance(message, ToolMessage)
        and message.tool_call_id == "call-approval-41"
        for message in state.values["messages"]
    )


FULL_USAGE = {
    "input_tokens": 4222,
    "output_tokens": 4,
    "total_tokens": 4226,
    "input_token_details": {"cache_creation": 0, "cache_read": 4210},
    "output_token_details": {"reasoning": 0},
}


class UsageModel(BaseChatModel):
    """Streaming model fixture whose callback usage is provider-shaped data."""

    usage: dict[str, Any]

    @property
    def _llm_type(self) -> str:
        return "zeroth-usage-scenario"

    def bind_tools(self, tools: Any, **kwargs: Any) -> UsageModel:
        del tools, kwargs
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        del messages, stop, run_manager, kwargs
        return ChatResult(
            generations=[
                ChatGeneration(message=AIMessage(content="ok", usage_metadata=self.usage))
            ]
        )

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        del messages, stop, run_manager, kwargs
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content="ok",
                usage_metadata=self.usage,
                chunk_position="last",
            )
        )

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        del messages, stop, run_manager, kwargs
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content="ok",
                usage_metadata=self.usage,
                chunk_position="last",
            )
        )


async def _stream_usage(usage: dict[str, Any], version: str) -> Any:
    graph = govern_graph(create_agent(model=UsageModel(usage=usage), tools=[]))
    stream = graph.astream_events(
        {"messages": [{"role": "user", "content": "Reply with ok"}]},
        version=version,
    )
    if inspect.isawaitable(stream):
        stream = await stream
    async for _event in stream:
        pass
    [observation] = graph.usage_observations
    return observation


@pytest.mark.asyncio
async def test_8094_supported_v3_preserves_provider_usage_details() -> None:
    """The supported pin keeps the detailed v2 and v3 callback payloads equivalent."""
    v2 = await _stream_usage(dict(FULL_USAGE), "v2")
    v3 = await _stream_usage(dict(FULL_USAGE), "v3")

    assert dict(v3.raw_usage) == dict(v2.raw_usage) == FULL_USAGE
    assert v2.cost_attribution_complete
    assert v3.cost_attribution_complete


@pytest.mark.asyncio
async def test_8094_lossy_v3_shape_refuses_complete_cost_attribution() -> None:
    """If v3 drops detail maps, Zeroth records missing provenance explicitly."""
    lossy = {
        "input_tokens": 4222,
        "output_tokens": 4,
        "total_tokens": 4226,
    }

    observation = await _stream_usage(lossy, "v3")

    assert dict(observation.raw_usage) == lossy
    assert not observation.cost_attribution_complete
    assert observation.missing_fields == (
        "input_token_details",
        "output_token_details",
    )
