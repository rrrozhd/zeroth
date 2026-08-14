from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, TypedDict

#: Marks an id this module minted because the provider supplied none.
#:
#: The prefix is the only channel through which the extraction site can tell a
#: consumer *how much identity the id carries*, because the id travels alone --
#: ``NormalizedToolCall`` is handed to the agent runner, which forwards nothing
#: but the string. A synthetic id names the call's **content**; a provider's id
#: names the call's **occurrence**. Anything that needs the second (
#: ``RuntimeToolExecutor``, which keys durable side-effect suppression) must
#: recognise the prefix and supply its own positional discriminator.
SYNTHETIC_CALL_ID_PREFIX = "zcall_"


class NormalizedToolCall(TypedDict):
    id: str
    name: str
    args: dict[str, Any]


def _canonical(value: Any, seen: set[int]) -> Any:
    """Rewrite a value into JSON-expressible form without consulting memory.

    Every branch is total and address-free: the point is that two processes
    handed equal values produce equal output. Values JSON cannot express at all
    collapse to their type name, which can merge two ids -- accepted, because a
    synthetic id is not what keys a side effect (see ``RuntimeToolExecutor``),
    while an address or a hash-ordered set *is* how the digest stopped being a
    function of the call.
    """
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if id(value) in seen:
        return ["cycle"]
    seen = seen | {id(value)}
    if isinstance(value, bytes | bytearray | memoryview):
        return ["bytes", bytes(value).hex()]
    if isinstance(value, Mapping):
        items = [
            [json.dumps(_canonical(key, seen)), _canonical(item, seen)]
            for key, item in value.items()
        ]
        # Sorted on the serialized key, so mixed-type keys order totally rather
        # than raising the way ``sort_keys=True`` does on ``{1: .., "b": ..}``.
        items.sort(key=lambda entry: entry[0])
        return ["map", items]
    if isinstance(value, set | frozenset):
        # A set has no order of its own, and the one it iterates in moves with
        # PYTHONHASHSEED -- sorting the serialized members is what pins it.
        return ["set", sorted(json.dumps(_canonical(item, seen)) for item in value)]
    if isinstance(value, list | tuple):
        return ["seq", [_canonical(item, seen) for item in value]]
    return ["opaque", f"{type(value).__module__}.{type(value).__qualname__}"]


def canonical_json(value: Any) -> str:
    """Serialize hash material so the digest is a function of the value alone.

    ``json.dumps(..., sort_keys=True, default=str)`` is not that function, and
    tool arguments are exactly where that bites: they arrive from a provider
    adapter, and a callable adapter may hand over any Python object.

    * ``default=str`` stringifies whatever it cannot serialize, so a plain
      object contributes its *memory address* and a ``set`` contributes an
      iteration order that moves with ``PYTHONHASHSEED``. Two processes then
      derive different digests for the same call -- the digest stops being an
      identity and the durable dedupe it feeds can never recognise a repeat.
    * ``sort_keys=True`` raises ``TypeError`` on a dict with mixed-type keys,
      so a total function became one that can abort an agent turn.

    Plain serialization is attempted first, so every JSON-safe payload -- which
    is all of them in practice -- keeps the byte-for-byte material it already
    had and no in-flight operation is re-keyed. Only what plain JSON cannot
    express falls through, under a prefix no JSON document can start with, so
    the fallback form cannot be forged by a payload taking the first branch.
    """
    try:
        return json.dumps(value, sort_keys=True)
    except (TypeError, ValueError):
        return "canonical/1:" + json.dumps(_canonical(value, set()))


def _synthetic_call_id(ordinal: int, name: str, args: dict[str, Any]) -> str:
    """Name an id-less tool call by its own content, never by chance.

    A provider that emits no call id still emits a *call*, and that call has to
    be nameable: ``build_tool_message`` needs a non-empty id to pair the result
    back to its request.

    Minting a random id broke replay: a fresh uuid4 per extraction meant the
    same replayed call derived a different operation key every time, so the
    durable dedupe could never recognise a repeat and silently never fired.
    Deriving the id from the call itself makes a replay of the same turn
    reproduce the same id -- while the ordinal keeps two same-name,
    same-argument calls in one turn apart, because those are two effects the
    model asked for, not one asked for twice.

    What this id deliberately does **not** do is tell one provider turn from
    another. The material available here is the message, and an agent's second
    turn can request a byte-identical call; nothing at this site distinguishes
    that from a replay of the first turn, since both re-derive from equal
    content. Turn position is known one layer down, where the tool executor
    counts calls within a node dispatch, so that is where it is applied --
    which is why the id is prefixed (``SYNTHETIC_CALL_ID_PREFIX``) rather than
    made to look provider-issued.
    """
    material = canonical_json([ordinal, name, args]).encode()
    return f"{SYNTHETIC_CALL_ID_PREFIX}{hashlib.sha256(material).hexdigest()[:24]}"


def _normalize_args(value: Any) -> dict[str, Any]:
    """Internal helper to normalize args.

    Deliberately *not* a sanitizer: the returned mapping is the payload handed
    to the tool, so coercing a value here (a ``set`` into a list, say) would
    change what the tool actually runs on. Making the hash material safe is
    ``canonical_json``'s job, and it only reads this mapping to derive a digest
    -- the payload itself travels through untouched.
    """
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
