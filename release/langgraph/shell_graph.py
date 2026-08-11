"""The smallest real LangGraph application, served by the real Agent Server.

This exists so the deployed acceptance stack has a genuine upstream to govern. It is
deliberately trivial in content — one node, one edge — and entirely genuine in
machinery: a real `StateGraph`, compiled by real LangGraph, served by
`langgraph_api.cli.run_server` at the version the compatibility gate pins.

That distinction is the whole point. What this replaces answered `/ok`, `/info` and
`/openapi.json` from hardcoded literals, and rebuilt its OpenAPI document out of the
very fixture the gateway's fingerprint pin was derived from — so the compatibility gate
was comparing the pin against its own answer key and reporting "supported" with no Agent
Server behaviour anywhere behind it. A minimal graph is a fixture; a server that recites
the expected fingerprint is not a server.

Keep this graph boring. Its job is to be somewhere for governed traffic to land, so the
gateway's admission, proxying, and streaming paths run against real protocol handling.
Anything clever here would be behaviour the acceptance suite might accidentally start
depending on.
"""

from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph


class ShellState(TypedDict, total=False):
    """The one value this application carries."""

    value: int
    echoed: str


async def echo(state: ShellState) -> ShellState:
    """Do the least a node can do while still genuinely executing."""
    value = int(state.get("value", 0))
    return {"value": value + 1, "echoed": str(value)}


def build_graph():
    """Compile the shell application."""
    builder = StateGraph(ShellState)
    builder.add_node("echo", echo)
    builder.add_edge(START, "echo")
    builder.add_edge("echo", END)
    return builder.compile()


graph = build_graph()
