"""Prompt assembly and audit serialization helpers.

This module builds the prompts that get sent to the AI model and provides
tools for creating audit-safe (redacted) records of what was sent and received.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from zeroth.runtime.agents.models import (
    AgentConfig,
    PromptAssembly,
    PromptMessage,
)
from zeroth.runtime.agents.sanitization import (
    HeuristicInjectionScreener,
    wrap_untrusted,
)


def _redact_value(value: Any, redact_keys: set[str]) -> Any:
    """Replace sensitive values in a nested structure with '***REDACTED***'.

    Walks through dicts, lists, and tuples. Any dict key that matches
    one of the redact_keys will have its value replaced.
    """
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if key in redact_keys:
                redacted[key] = "***REDACTED***"
            else:
                redacted[key] = _redact_value(item, redact_keys)
        return redacted
    if isinstance(value, list):
        return [_redact_value(item, redact_keys) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item, redact_keys) for item in value]
    return value


def _json_block(label: str, payload: Any) -> str:
    """Format a label and a JSON-serializable value as a readable text block."""
    return f"{label}\n{json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)}"


class PromptAssembler:
    """Builds the messages that get sent to the AI model.

    Takes the agent config, the user's input data, and optional thread state,
    then constructs a system message and user message with all the relevant
    context the model needs to produce a good response.
    """

    def assemble(
        self,
        config: AgentConfig,
        input_payload: BaseModel | Mapping[str, Any],
        *,
        thread_state: Mapping[str, Any] | None = None,
        runtime_context: Mapping[str, Any] | None = None,
    ) -> PromptAssembly:
        """Build a complete prompt from the agent config and input data.

        Combines the agent's instruction, schemas, tools, memory refs,
        input payload, and thread state into a structured set of messages.
        Sensitive fields are redacted based on the prompt config.
        """
        validated = self._normalize_input(config, input_payload)
        prompt_config = config.prompt_config
        redact_keys = set(prompt_config.redact_keys)
        input_dump = _redact_value(validated.model_dump(mode="json"), redact_keys)
        thread_dump = _redact_value(dict(thread_state or {}), set(prompt_config.redact_keys))
        context_dump = _redact_value(dict(runtime_context or {}), set(prompt_config.redact_keys))

        # Internal bookkeeping keys must never be RENDERED back into the prompt
        # (audit B1). The persisted per-turn "audit" record embeds the prior
        # turn's full rendered_prompt (which itself carries a thread-state block),
        # so re-injecting it here made every turn nest the last — geometric prompt
        # growth (~5x/turn, context-window blowup within a few turns). Strip these
        # from the copy used for the "Thread state:" block and the audit metadata;
        # the runner still reads the real thread_state (compacted_messages, etc.)
        # directly from checkpoint state, so continuity is unaffected.
        if isinstance(thread_dump, dict):
            for _internal_key in ("audit", "compacted_messages", "archived_messages"):
                thread_dump.pop(_internal_key, None)

        # Conversation input: the configured payload field is lifted out of the
        # input JSON block and rendered as real chat turns after the user
        # message. The audit metadata keeps the raw (redacted) list.
        conversation_items: list[Any] = []
        if (
            prompt_config.messages_key
            and isinstance(input_dump, dict)
            and isinstance(input_dump.get(prompt_config.messages_key), list)
        ):
            conversation_items = input_dump.pop(prompt_config.messages_key)

        # Persistent conversation: turns stored in thread state replay before
        # the incoming ones, so a caller submitting the same thread_id only
        # sends what is new. Lifted out of the thread-state block to avoid
        # rendering the same turns twice.
        stored_turns = 0
        if (
            prompt_config.messages_key
            and prompt_config.persist_conversation
            and isinstance(thread_dump, dict)
            and isinstance(thread_dump.get("conversation"), list)
        ):
            stored = thread_dump.pop("conversation")
            stored_turns = len(stored)
            conversation_items = stored + conversation_items
        if prompt_config.conversation_max_turns is not None:
            conversation_items = conversation_items[-prompt_config.conversation_max_turns :]

        system_parts = [
            f"Agent: {config.name}",
            f"Instruction: {config.instruction}",
            f"Model: {config.model_name}",
        ]
        if config.description:
            system_parts.append(f"Description: {config.description}")
        if prompt_config.include_tool_refs and config.declared_tool_refs:
            system_parts.append(f"Allowed tools: {', '.join(config.declared_tool_refs)}")
        if prompt_config.include_memory_refs and config.memory_refs:
            system_parts.append(f"Memory refs: {', '.join(config.memory_refs)}")
        if prompt_config.include_input_schema:
            system_parts.append(
                _json_block("Input schema:", config.input_model.model_json_schema())
            )
        if prompt_config.include_output_schema:
            system_parts.append(
                _json_block("Output schema:", config.output_model.model_json_schema())
            )

        user_parts = [_json_block("Input payload:", input_dump)]
        if prompt_config.include_thread_state:
            user_parts.append(_json_block("Thread state:", thread_dump))
        # MBND-02: when safety is enabled, memory-sourced content is untrusted —
        # split it out of the trusted runtime context and render it (already
        # redacted, and screened) in a clearly delimited provenance block so the
        # model treats it as data, not instructions. When safety is disabled,
        # memory stays nested in the runtime context exactly as before.
        safety = config.tool_output_safety
        display_context = dict(context_dump)
        if safety.enabled and "memory" in display_context:
            memory_text = json.dumps(
                display_context.pop("memory"), indent=2, sort_keys=True, ensure_ascii=False
            )
            memory_flags = (
                HeuristicInjectionScreener().screen(memory_text)
                if safety.screen_for_injection
                else ()
            )
            user_parts.append(wrap_untrusted(memory_text, source="memory", flags=memory_flags))
        if display_context:
            user_parts.append(_json_block("Runtime context:", display_context))
        if prompt_config.extra_context:
            user_parts.append(_json_block("Prompt context:", prompt_config.extra_context))

        messages = [
            PromptMessage(role="system", content="\n\n".join(system_parts)),
            PromptMessage(role="user", content="\n\n".join(user_parts)),
        ]
        messages.extend(self._conversation_messages(config, conversation_items))
        metadata = {
            "agent_name": config.name,
            "model_name": config.model_name,
            "input_payload": input_dump,
            "thread_state": thread_dump,
            "runtime_context": context_dump,
            "tool_refs": list(config.declared_tool_refs),
            "memory_refs": list(config.memory_refs),
        }
        if conversation_items:
            metadata["conversation_messages"] = conversation_items
        if stored_turns:
            metadata["conversation_stored_turns"] = stored_turns
        return PromptAssembly(
            messages=messages,
            rendered_prompt="\n\n".join(message.content for message in messages),
            metadata=metadata,
        )

    def _conversation_messages(
        self,
        config: AgentConfig,
        items: list[Any],
    ) -> list[PromptMessage]:
        """Map incoming chat items ({role, content}) to prompt messages.

        ``human`` becomes a user turn, ``ai`` an assistant turn. ``tool``
        content is externally produced, so it is rendered as an untrusted
        provenance block (screened when safety is on) rather than replayed
        as a bare turn.
        """
        safety = config.tool_output_safety
        screener = (
            HeuristicInjectionScreener() if safety.enabled and safety.screen_for_injection else None
        )
        result: list[PromptMessage] = []
        for item in items:
            if not isinstance(item, Mapping):
                result.append(
                    PromptMessage(role="user", content=json.dumps(item, ensure_ascii=False))
                )
                continue
            role = str(item.get("role", "human")).lower()
            content = item.get("content", "")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False, sort_keys=True)
            if role in ("ai", "assistant"):
                result.append(PromptMessage(role="assistant", content=content))
            elif role == "tool":
                name = item.get("name")
                source = f"tool:{name}" if name else "tool"
                if safety.enabled:
                    flags = screener.screen(content) if screener else ()
                    content = wrap_untrusted(content, source=source, flags=flags)
                else:
                    content = f"[{source} result]\n{content}"
                result.append(PromptMessage(role="user", content=content))
            else:
                result.append(PromptMessage(role="user", content=content))
        return result

    def _normalize_input(
        self,
        config: AgentConfig,
        input_payload: BaseModel | Mapping[str, Any],
    ) -> BaseModel:
        """Convert the input payload into the agent's expected input model."""
        if isinstance(input_payload, BaseModel):
            return config.input_model.model_validate(input_payload.model_dump(mode="json"))
        return config.input_model.model_validate(input_payload)


def _cache_disposition(serialized_response: Mapping[str, Any]) -> str | None:
    """Name a serialized response's cache outcome as an audit-retained label.

    Args:
        serialized_response: The redacted provider response.

    Returns:
        ``"cache_hit"`` or ``"cache_miss"`` when the caching adapter was in the
        chain, else ``None``.
    """
    metadata = serialized_response.get("metadata")
    if not isinstance(metadata, Mapping) or "cache_hit" not in metadata:
        return None
    return "cache_hit" if metadata["cache_hit"] else "cache_miss"


class AgentAuditSerializer:
    """Creates audit-safe copies of prompts and responses.

    Replaces sensitive values (like passwords or tokens) with placeholder
    text so the audit log does not contain secrets.
    """

    def __init__(self, *, redact_keys: set[str] | None = None) -> None:
        self._redact_keys = redact_keys or set()

    def serialize_prompt(self, assembly: PromptAssembly) -> dict[str, Any]:
        """Turn a prompt assembly into a redacted dictionary for logging."""
        return {
            "messages": [
                {
                    "role": message.role,
                    "content": self._redact_text(message.content),
                }
                for message in assembly.messages
            ],
            "rendered_prompt": self._redact_text(assembly.rendered_prompt),
            "metadata": self._redact_structure(assembly.metadata),
        }

    def serialize_response(self, response: Any) -> dict[str, Any]:
        """Turn a provider response into a redacted dictionary for logging."""
        if hasattr(response, "model_dump"):
            payload = response.model_dump(mode="json")
        elif isinstance(response, Mapping):
            payload = dict(response)
        else:
            payload = {"value": response}
        return self._redact_structure(payload)

    def serialize_record(
        self,
        *,
        prompt: PromptAssembly,
        response: Any,
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        """Combine prompt, response, and extra info into one redacted audit record.

        The nested ``prompt``/``response``/``extra`` sections are content and do
        not survive the audit capture boundary's metadata-only default. The two
        signals that are *not* content -- how many attempts the call took and
        whether the provider response came from the cache -- are therefore also
        promoted to flat, allowlisted keys, so the econ waste detectors still
        see them in a persisted record.
        """
        serialized_response = self.serialize_response(response)
        record: dict[str, Any] = {
            "prompt": self.serialize_prompt(prompt),
            "response": serialized_response,
            "extra": self._redact_structure(extra),
        }
        attempts = extra.get("attempts")
        if type(attempts) is int:
            record["attempt"] = attempts
        disposition = _cache_disposition(serialized_response)
        if disposition is not None:
            record["disposition"] = disposition
        return record

    def _redact_structure(self, value: Any) -> Any:
        """Redact sensitive keys in a nested data structure."""
        return _redact_value(value, self._redact_keys)

    def _redact_text(self, text: str) -> str:
        """Replace occurrences of sensitive key names in plain text."""
        redacted = text
        for key in self._redact_keys:
            redacted = redacted.replace(key, "***REDACTED***")
        return redacted
