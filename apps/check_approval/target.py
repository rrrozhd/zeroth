from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langchain_core.messages import BaseMessage
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph, add_messages
from langgraph.prebuilt import ToolNode

from zeroth.check.adapter.bindings import CheckBindings
from zeroth.check.adapter.langgraph import LangGraphCheckTarget
from zeroth.integrations.langgraph import (
    SideEffectClass,
    ToolDecision,
    ToolDecisionKind,
    ToolGovernanceContext,
    govern_tools,
)


class State(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


@dataclass
class ApprovedResumeFixture:
    """Deterministic stand-in for a previously persisted human approval receipt."""

    def decide(self, action: Any, context: Any) -> ToolDecision:
        del action, context
        return ToolDecision(ToolDecisionKind.ALLOW, "unknown_error")


def build_target(bindings: CheckBindings) -> LangGraphCheckTarget:
    @tool
    def apply_approved_refund(invoice_id: str) -> dict[str, str]:
        """Apply an already-approved local refund in consented record mode."""
        ledger = Path(".zeroth/check/apps/approval-ledger.txt")
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8") as handle:
            handle.write(f"{invoice_id}\n")
        return {"receipt": f"refunded-{invoice_id}"}

    selected = bindings.tool("apply_approved_refund", apply_approved_refund, "side_effecting")
    [governed] = govern_tools(
        [selected],
        context=ToolGovernanceContext(
            tenant_id="check-reference",
            principal_id="approval-fixture",
            run_id="approval-invoice-7",
            thread_id="approval-invoice-7",
        ),
        client=ApprovedResumeFixture(),
        side_effect=lambda _tool: SideEffectClass.SIDE_EFFECTING,
        action_lifecycle=bindings.action_repository,
    )

    def graph_factory(checkpointer: SqliteSaver):
        builder = StateGraph(State)
        builder.add_node("approved_action", ToolNode([governed]))
        builder.add_edge(START, "approved_action")
        builder.add_edge("approved_action", END)
        return builder.compile(checkpointer=checkpointer)

    @contextmanager
    def checkpointer_factory(path: Path) -> Iterator[SqliteSaver]:
        with SqliteSaver.from_conn_string(str(path)) as saver:
            yield saver

    return LangGraphCheckTarget(
        graph_factory=graph_factory,
        checkpointer_factory=checkpointer_factory,
        case_input=lambda case: {
            "messages": [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "name": "apply_approved_refund",
                            "args": {"invoice_id": "invoice-7"},
                            "id": "call-approval-7",
                            "type": "tool_call",
                        }
                    ],
                }
            ]
        },
        invocation_config=lambda case, run: {"configurable": {"thread_id": f"{case}:{run}"}},
    )
