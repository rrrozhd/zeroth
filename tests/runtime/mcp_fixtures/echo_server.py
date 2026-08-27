"""A minimal real MCP server, spawned over stdio by the transport tests.

Every other MCP test in this repository patches ``stdio_client`` and
``ClientSession``, so the actual transport -- process spawn, handshake,
``list_tools``, ``call_tool``, shutdown -- had never run. A mock cannot tell you
that the deadline plumbing works, that a tool's schema survives the wire in the
form the digest was taken over, or that a spawned process is really reaped.

Kept deliberately tiny and dependency-free beyond ``mcp`` itself: it is a
control surface for the tests, not an example.

``ZEROTH_FIXTURE_DRIFT=1`` makes it advertise a *different* description for
``echo``, which is how the drift test gets a server whose shape changed between
import and run without needing two files.
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

server = FastMCP("zeroth-test-echo")

_DRIFTED = os.environ.get("ZEROTH_FIXTURE_DRIFT") == "1"


@server.tool(description="Echo the text back" if not _DRIFTED else "Echo the text back (v2)")
def echo(text: str) -> str:
    """Return *text* unchanged."""
    return text


@server.tool(description="Add two integers")
def add(a: int, b: int) -> int:
    """Return the sum of *a* and *b*."""
    return a + b


if __name__ == "__main__":
    server.run()
