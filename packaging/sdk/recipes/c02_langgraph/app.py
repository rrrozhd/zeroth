"""A representative LangGraph/LangChain application. It does not import Zeroth.

Models are injected so the same graphs run against real providers or fakes.
"""

from __future__ import annotations

import json
import operator
from typing import Annotated, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt


class State(TypedDict):
    question: str
    parts: Annotated[list[str], operator.add]
    approved: bool


def plan_then_answer(planner, writer):
    """Two sequential model steps."""
    graph = StateGraph(State)
    graph.add_node("plan", lambda s: {"parts": [planner.invoke(s["question"]).content]})
    graph.add_node("answer", lambda s: {"parts": [writer.invoke(s["question"]).content]})
    graph.add_edge(START, "plan")
    graph.add_edge("plan", "answer")
    graph.add_edge("answer", END)
    return graph.compile()


def single(model, node: str = "answer"):
    graph = StateGraph(State)
    graph.add_node(node, lambda s: {"parts": [model.invoke(s["question"]).content]})
    graph.add_edge(START, node)
    graph.add_edge(node, END)
    return graph.compile()


def fanout_then_merge(planner, merger):
    """Parallel branches through Send, then a merge step."""
    graph = StateGraph(State)
    graph.add_node("fanout", lambda s: {"parts": []})
    graph.add_node("branch", lambda s: {"parts": [planner.invoke(s["question"]).content]})
    graph.add_node("merge", lambda s: {"parts": [merger.invoke("merge " + s["question"]).content]})
    graph.add_edge(START, "fanout")
    graph.add_conditional_edges(
        "fanout",
        lambda s: [Send("branch", {**s, "question": f"{s['question']} {i}"}) for i in ("a", "b")],
        ["branch"],
    )
    graph.add_edge("branch", "merge")
    graph.add_edge("merge", END)
    return graph.compile()


def reviewed_pipeline(planner, writer, quick):
    """Plan, fan out, merge, wait for approval, run a subgraph, finish: six model calls."""
    sub = StateGraph(State)
    sub.add_node("draft", lambda s: {"parts": [writer.invoke("draft " + s["question"]).content]})
    sub.add_edge(START, "draft")
    sub.add_edge("draft", END)

    graph = StateGraph(State)
    graph.add_node("plan", lambda s: {"parts": [planner.invoke(s["question"]).content]})
    graph.add_node("branch", lambda s: {"parts": [planner.invoke(s["question"]).content]})
    graph.add_node("merge", lambda s: {"parts": [quick.invoke("merge").content]})
    graph.add_node("gate", lambda s: {"approved": bool(interrupt("approve?"))})
    graph.add_node("sub", sub.compile())
    graph.add_node("final", lambda s: {"parts": [planner.invoke("final").content]})
    graph.add_edge(START, "plan")
    graph.add_conditional_edges(
        "plan", lambda s: [Send("branch", {**s, "question": q}) for q in ("a", "b")], ["branch"]
    )
    graph.add_edge("branch", "merge")
    graph.add_edge("merge", "gate")
    graph.add_edge("gate", "sub")
    graph.add_edge("sub", "final")
    graph.add_edge("final", END)
    return graph.compile(checkpointer=InMemorySaver())


def robust_extract(model, text: str) -> dict:
    """Application-level retry: one more attempt when the model returns invalid JSON."""
    for attempt in (1, 2):
        reply = model.invoke(f"Return JSON for: {text}").content
        try:
            return json.loads(reply)
        except ValueError:
            if attempt == 2:
                raise
    raise AssertionError("unreachable")


def with_fallback(primary, fallback):
    return primary.with_fallbacks([fallback])


def stream_text(model, question: str) -> str:
    return "".join(chunk.content or "" for chunk in model.stream(question))


def initial(question: str) -> State:
    return {"question": question, "parts": [], "approved": False}
