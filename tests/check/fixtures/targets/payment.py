from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from zeroth.check.adapter.bindings import CheckBindings
from zeroth.check.adapter.langgraph import LangGraphCheckTarget


class State(TypedDict):
    value: int


def build_target(bindings: CheckBindings) -> LangGraphCheckTarget:
    def increment(value: int) -> int:
        return value + 1

    selected = bindings.tool("increment", increment, "read_only")

    def graph_factory(checkpointer: SqliteSaver):
        builder = StateGraph(State)
        builder.add_node("increment", lambda state: {"value": selected(state["value"])})
        builder.add_edge(START, "increment")
        builder.add_edge("increment", END)
        return builder.compile(checkpointer=checkpointer)

    @contextmanager
    def checkpointer_factory(path: Path) -> Iterator[SqliteSaver]:
        with SqliteSaver.from_conn_string(str(path)) as saver:
            yield saver

    return LangGraphCheckTarget(
        graph_factory=graph_factory,
        checkpointer_factory=checkpointer_factory,
        case_input=lambda case: {"value": int(case)},
        invocation_config=lambda case, scenario_run_id: {
            "configurable": {"thread_id": f"{case}:{scenario_run_id}"}
        },
    )
