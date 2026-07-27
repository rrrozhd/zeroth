"""The scripted agent both tool-governance suites drive a real ``ToolNode`` through.

Some governance properties are only observable when ``ToolNode`` runs the call
rather than a stand-in: argument *injection* is the one this module exists for.
``ToolNode`` resolves an ``InjectedState`` / ``InjectedStore`` /
``InjectedToolCallId`` argument itself, after the middleware chain has returned,
so a test that hands a tool its arguments directly can never see one. Only a
model-driven ``create_agent`` invocation can.

One definition, used by both suites -- the same reason
:class:`~tests.integrations.langgraph.tools._hostile.HostileStr` is imported
rather than redefined. ``test_middleware`` and ``test_surface_parity`` both need
a model that asks for exactly one tool call, and two copies of a fake model are
two things to keep in step.

These need ``langchain``, so importing this module is itself Tier A: the
importing suite carries the ``importorskip`` and the ``langgraph_conformance``
marker, and this module assumes both have already been established.
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

__all__ = ["TOOL_CALL_ID", "ToolCallingModel", "scripted_model"]

TOOL_CALL_ID = "call-1"
"""The id every scripted call carries.

Written down rather than generated because it is *observable governance state*: a
tool declaring an ``InjectedToolCallId`` argument is decided against this exact
string, so a scenario can assert the decided arguments literally.
"""


class ToolCallingModel(GenericFakeChatModel):
    """A fake chat model that emits scripted tool calls and accepts a tool binding."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        """Accept the binding unchanged: the scripted messages already name the tool."""
        return self


def scripted_model(tool_name: str, arguments: dict[str, Any]) -> ToolCallingModel:
    """Build a model that asks for one tool call and then answers.

    Args:
        tool_name: The tool the single scripted call names.
        arguments: The arguments the model emits, before any validation or
            injection. Injected arguments are deliberately absent: the model
            never supplies one, which is the whole reason they are a distinct
            case.

    Returns:
        A model that asks for one tool call and then stops.
    """
    return ToolCallingModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[{"name": tool_name, "args": arguments, "id": TOOL_CALL_ID}],
                ),
                AIMessage(content="done"),
            ]
        )
    )
