from __future__ import annotations

import os
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
class AllowClient:
    def decide(self, action: Any, context: Any) -> ToolDecision:
        del action, context
        return ToolDecision(ToolDecisionKind.ALLOW, "unknown_error")


def build_target(bindings: CheckBindings) -> LangGraphCheckTarget:
    @tool
    def charge(amount: int) -> dict[str, object]:
        """A live implementation that replay must never reach."""

        marker = os.environ.get("ZEROTH_CHECK_LIVE_MARKER")
        if marker:
            Path(marker).write_text("LIVE TOOL EXECUTED")
        raise RuntimeError("LiveToolExecuted")

    selected = bindings.tool("charge", charge, "side_effecting")
    [selected] = govern_tools(
        [selected],
        context=ToolGovernanceContext(
            tenant_id="zeroth-check",
            principal_id="fixture",
            run_id="7",
            thread_id="check:7",
        ),
        client=AllowClient(),
        side_effect=lambda _tool: SideEffectClass.SIDE_EFFECTING,
        action_lifecycle=bindings.action_repository,
    )

    def graph_factory(checkpointer: SqliteSaver):
        builder = StateGraph(State)
        builder.add_node("tools", ToolNode([selected]))
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
                            "args": {"amount": int(case)},
                            "id": "call-charge-1",
                            "type": "tool_call",
                        }
                    ],
                }
            ]
        },
        invocation_config=lambda case, run: {"configurable": {"thread_id": f"{case}:{run}"}},
    )
