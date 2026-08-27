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
class AllowLocalFixture:
    def decide(self, action: Any, context: Any) -> ToolDecision:
        del action, context
        return ToolDecision(ToolDecisionKind.ALLOW, "unknown_error")


def build_target(bindings: CheckBindings) -> LangGraphCheckTarget:
    @tool
    def charge(amount: int, currency: str) -> dict[str, object]:
        """Append one local payment marker in explicitly consented record mode."""
        ledger = Path(".zeroth/check/apps/payment-ledger.txt")
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8") as handle:
            handle.write(f"{amount}:{currency}\n")
        return {"receipt": f"local-{amount}-{currency}"}

    selected = bindings.tool("charge", charge, "side_effecting")
    [governed] = govern_tools(
        [selected],
        context=ToolGovernanceContext(
            tenant_id="check-reference",
            principal_id="payment-fixture",
            run_id="payment-7",
            thread_id="payment-7",
        ),
        client=AllowLocalFixture(),
        side_effect=lambda _tool: SideEffectClass.SIDE_EFFECTING,
        action_lifecycle=bindings.action_repository,
    )

    def graph_factory(checkpointer: SqliteSaver):
        builder = StateGraph(State)
        builder.add_node("tools", ToolNode([governed]))
        builder.add_edge(START, "tools")
        builder.add_edge("tools", END)
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
                            "name": "charge",
                            "args": {"amount": 7, "currency": "USD"},
                            "id": "call-payment-7",
                            "type": "tool_call",
                        }
                    ],
                }
            ]
        },
        invocation_config=lambda case, run: {"configurable": {"thread_id": f"{case}:{run}"}},
    )
