"""33 — MCP tools: attach an external MCP server to an AgentNode.

What this shows
---------------
MCP (Model Context Protocol) servers expose tool suites over stdio.
Zeroth's :class:`AgentConfig` accepts ``mcp_servers=[MCPServerConfig(...)]``
and the :class:`AgentRunner` auto-discovers their tools at start-up,
merging them into the :class:`ToolAttachmentRegistry` alongside any
native tools. This file builds the MCPServerConfig and hands it to the
runner so you can see the exact wiring shape.

This example does **not** spin up a real MCP server — that would pull
a non-hermetic dependency into the examples suite. Instead it
validates the config shape, checks whether the ``mcp`` package is
installed, and prints a pointer to the real wiring code in
:mod:`zeroth.core.agent_runtime.mcp`. Replace the config command with
a real ``npx @modelcontextprotocol/server-filesystem /tmp`` (or
similar) to exercise the full discovery flow against a real server.

Run
---
    uv run python examples/33_mcp_tools.py
"""

from __future__ import annotations

# Allow python examples/NN_name.py to find the sibling examples/_common.py helper.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import importlib.util
import sys

from examples._contracts import Answer, Question
from zeroth.core.agent_runtime import (
    AgentConfig,
    AgentRunner,
    DeterministicProviderAdapter,
    MCPServerConfig,
    ProviderResponse,
)


def main() -> int:
    # 1. Real Pydantic config. This is what you'd commit to a graph's
    #    AgentNodeData.mcp_servers list in production.
    fs_server = MCPServerConfig(
        name="filesystem",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        env=None,
    )
    print(f"MCP server config: name={fs_server.name!r} command={fs_server.command!r}")
    print(f"  args: {fs_server.args}")

    # 2. Wire it onto an AgentConfig. The runner discovers tools on
    #    first use via MCPClientManager.start() — see
    #    zeroth/core/agent_runtime/mcp.py:44 for the discovery loop.
    config = AgentConfig(
        name="fs-agent",
        description="Agent with filesystem MCP tools.",
        instruction="Use the filesystem MCP server to answer questions.",
        model_name="openai/gpt-4o-mini",
        input_model=Question,
        output_model=Answer,
        mcp_servers=[fs_server],
    )
    runner = AgentRunner(
        config,
        DeterministicProviderAdapter(
            responses=[ProviderResponse(content={"answer": "mcp config validated"})]
        ),
    )
    print(f"AgentRunner built with {len(runner.config.mcp_servers)} MCP server(s)")

    # 3. The ``mcp`` SDK is an optional dependency. If it's not
    #    installed, the runner will only connect when a real call
    #    reaches ``_connect_mcp_servers``. Tell the reader whether it
    #    would actually wire up on this machine.
    mcp_available = importlib.util.find_spec("mcp") is not None
    print(f"\nmcp package available: {mcp_available}")
    if mcp_available:
        print("  → runner.start() would invoke MCPClientManager.start()")
        print("  → tools declared by the server would be added to the runner's tool registry")
    else:
        print("  → install `mcp` and point ``command``+``args`` at a real server to exercise discovery")
    print("\nSee src/zeroth/core/agent_runtime/mcp.py for the discovery / call flow.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
