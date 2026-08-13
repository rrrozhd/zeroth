from __future__ import annotations

import hashlib
import json
from typing import Any, TypedDict


class NormalizedToolCall(TypedDict):
    id: str
    name: str
    args: dict[str, Any]


def _synthetic_call_id(ordinal: int, name: str, args: dict[str, Any]) -> str:
    """Name an id-less tool call by its own content, never by chance.

    A provider that emits no call id still emits a *call*, and that call has to
    be nameable: ``RuntimeToolExecutor`` keys the side-effect operation on the
    id whenever one is present, and ``build_tool_message`` needs a non-empty id
    to pair the result back to its request.

    Minting a random id satisfied both and broke the first: a fresh uuid4 per
    extraction meant the same replayed call derived a different operation key
    every time, so the durable dedupe could never recognise a repeat and silently
    never fired. Deriving the id from the call itself makes a replay of the same
    turn reproduce the same id -- while the ordinal keeps two same-name,
    same-argument calls in one turn apart, because those are two effects the
    model asked for, not one asked for twice.
    """
    material = json.dumps([ordinal, name, args], sort_keys=True, default=str).encode()
    return f"zcall_{hashlib.sha256(material).hexdigest()[:24]}"


def _normalize_args(value: Any) -> dict[str, Any]:
    """Internal helper to normalize args."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return {"value": value}
        if isinstance(parsed, dict):
            return parsed
        return {"value": parsed}
    return {}


def extract_tool_calls(ai_message: Any) -> list[NormalizedToolCall]:
    """Extract tool calls."""
    tool_calls = getattr(ai_message, "tool_calls", None)
    if tool_calls is None and isinstance(ai_message, dict):
        tool_calls = ai_message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return []

    out: list[NormalizedToolCall] = []
    for raw in tool_calls:
        if not isinstance(raw, dict):
            continue
        raw_id = raw.get("id")
        name = raw.get("name")
        args = raw.get("args", {})
        if not isinstance(name, str) or not name:
            continue
        normalized_args = _normalize_args(args)
        out.append(
            {
                "id": (
                    str(raw_id) if raw_id else _synthetic_call_id(len(out), name, normalized_args)
                ),
                "name": name,
                "args": normalized_args,
            }
        )
    return out


def build_tool_message(
    *, tool_call_id: str, name: str, content: str, is_error: bool = False
) -> Any:
    """Build tool message."""
    try:
        from langchain_core.messages import ToolMessage  # type: ignore
    except Exception:
        return {
            "type": "tool",
            "tool_call_id": tool_call_id,
            "name": name,
            "content": content,
            "is_error": is_error,
        }

    kwargs: dict[str, Any] = {}
    if is_error:
        kwargs["is_error"] = True
    return ToolMessage(
        content=content,
        tool_call_id=tool_call_id,
        name=name,
        additional_kwargs=kwargs,
    )


class GovernedToolCallLoop:
    """Execute AIMessage tool calls through GovernAI runtime governance."""

    async def execute_once(
        self,
        *,
        runtime: Any,
        workflow: Any,
        state: Any,
        step_name: str,
        ai_message: Any,
    ) -> list[Any]:
        """Execute once."""
        messages: list[Any] = []
        for tool_call in extract_tool_calls(ai_message):
            try:
                result = await runtime.execute_named_tool(
                    state=state,
                    workflow=workflow,
                    step_name=step_name,
                    tool_name=tool_call["name"],
                    payload=tool_call["args"],
                )
                content = json.dumps(result, ensure_ascii=False)
                messages.append(
                    build_tool_message(
                        tool_call_id=tool_call["id"],
                        name=tool_call["name"],
                        content=content,
                    )
                )
            except Exception as exc:
                messages.append(
                    build_tool_message(
                        tool_call_id=tool_call["id"],
                        name=tool_call["name"],
                        content=str(exc),
                        is_error=True,
                    )
                )
        return messages

