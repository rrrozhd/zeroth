"""Manifest metadata keys shared between the runner factory and the runner.

Kept out of ``factory`` itself because that module imports ``AgentRunner``; the
runner importing back would close a cycle. A key both sides must agree on is
exactly the kind of thing that belongs in neither.
"""

from __future__ import annotations

#: Marks a tool attachment whose call never reaches the side-effect operation
#: boundary. The factory stamps it when a binding targets an ``mcp_tool`` node;
#: the runner reads it to set ``at_least_once`` on the tool-call audit record.
#: An unmarked record would read as though the operation guarantee applied.
MCP_AT_LEAST_ONCE = "mcp_at_least_once"
