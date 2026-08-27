from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph, add_messages
from langgraph.prebuilt import ToolNode

from zeroth.check.adapter.bindings import CheckBindings
from zeroth.check.adapter.langgraph import LangGraphCheckTarget


class State(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


def build_target(bindings: CheckBindings) -> LangGraphCheckTarget:
    @tool
    def charge(amount: int) -> dict[str, object]:
        """Create a fixture charge."""

        return {"charged": amount}

    selected = bindings.tool("charge", charge, "side_effecting")

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
