from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy, interrupt


class FixtureState(TypedDict, total=False):
    mode: str
    text: str
    result: str
    resumed: Any
    model_request: dict[str, Any]
    model_response: dict[str, Any]
    retry_attempts: list[int]
    retry_key: str
    tool_calls: list[dict[str, Any]]
    tool_sequence: list[dict[str, Any]]


_CASSETTE_PATH = Path(__file__).with_name("cassettes") / "deterministic.json"


def _cassette() -> dict[str, Any]:
    return json.loads(_CASSETTE_PATH.read_text(encoding="utf-8"))


def echo(state: FixtureState) -> FixtureState:
    writer = get_stream_writer()
    text = state.get("text", "")
    writer({"kind": "custom", "sequence": 1, "value": "echo:start"})
    writer({"kind": "token", "sequence": 2, "value": text})
    writer({"kind": "custom", "sequence": 3, "value": "echo:end"})
    return {"result": f"echo:{text}"}


def pause(state: FixtureState) -> FixtureState:
    resumed = interrupt(
        {
            "kind": "fixture-approval",
            "prompt": state.get("text", "approve"),
            "schema_version": 1,
        }
    )
    return {"resumed": resumed, "result": f"resumed:{resumed}"}


async def cancellation_point(_: FixtureState) -> FixtureState:
    await asyncio.sleep(30)
    return {"result": "cancellation-point-completed"}


def predictable_error(_: FixtureState) -> FixtureState:
    raise RuntimeError("deterministic-fixture-error")


def replay_tools(state: FixtureState) -> FixtureState:
    recordings = _cassette()["interactions"]
    sequence: list[dict[str, Any]] = []
    for requested in state.get("tool_calls", []):
        match = next(
            (
                row
                for row in recordings
                if row["name"] == requested.get("name")
                and row["arguments"] == requested.get("arguments")
            ),
            None,
        )
        if match is None:
            raise AssertionError(f"cassette miss: {requested!r}")
        sequence.append(
            {
                "name": match["name"],
                "arguments": match["arguments"],
                "result": match["result"],
            }
        )
    return {"tool_sequence": sequence, "result": "tools:replayed"}


def replay_model(state: FixtureState) -> FixtureState:
    requested = state.get("model_request")
    match = next(
        (
            row
            for row in _cassette()["model_interactions"]
            if row["request"] == requested
        ),
        None,
    )
    if match is None:
        raise AssertionError("model cassette miss")
    return {"model_response": match["response"], "result": "model:replayed"}


# ponytail: process-local fixture state; use run-scoped storage if retry cases become concurrent.
_retry_attempts: dict[str, int] = {}


def retry_once(state: FixtureState) -> FixtureState:
    key = state.get("retry_key", "default")
    attempt = _retry_attempts.get(key, 0) + 1
    _retry_attempts[key] = attempt
    if attempt == 1:
        raise RuntimeError("deterministic-retry")
    _retry_attempts.pop(key, None)
    return {"retry_attempts": [1, 2], "result": "retry:succeeded"}


def route(state: FixtureState) -> str:
    return state.get("mode", "echo")


builder = StateGraph(FixtureState)
builder.add_node("echo", echo)
builder.add_node("interrupt", pause)
builder.add_node("cancel", cancellation_point)
builder.add_node("error", predictable_error)
builder.add_node("model", replay_model)
builder.add_node(
    "retry",
    retry_once,
    retry_policy=RetryPolicy(
        initial_interval=0.0,
        max_attempts=2,
        jitter=False,
        retry_on=RuntimeError,
    ),
)
builder.add_node("tools", replay_tools)
builder.add_conditional_edges(
    START,
    route,
    {
        "echo": "echo",
        "interrupt": "interrupt",
        "cancel": "cancel",
        "error": "error",
        "model": "model",
        "retry": "retry",
        "tools": "tools",
    },
)
for node in ("echo", "interrupt", "cancel", "error", "model", "retry", "tools"):
    builder.add_edge(node, END)

graph = builder.compile()
