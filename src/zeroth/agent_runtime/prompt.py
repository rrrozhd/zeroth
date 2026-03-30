"""Prompt assembly and audit serialization helpers.

This module builds the prompts that get sent to the AI model and provides
tools for creating audit-safe (redacted) records of what was sent and received.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from zeroth.agent_runtime.models import (
    AgentConfig,
    PromptAssembly,
    PromptMessage,
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
        if context_dump:
            user_parts.append(_json_block("Runtime context:", context_dump))
        if prompt_config.extra_context:
            user_parts.append(_json_block("Prompt context:", prompt_config.extra_context))

        messages = [
            PromptMessage(role="system", content="\n\n".join(system_parts)),
            PromptMessage(role="user", content="\n\n".join(user_parts)),
        ]
        return PromptAssembly(
            messages=messages,
            rendered_prompt="\n\n".join(message.content for message in messages),
            metadata={
                "agent_name": config.name,
                "model_name": config.model_name,
                "input_payload": input_dump,
                "thread_state": thread_dump,
                "runtime_context": context_dump,
                "tool_refs": list(config.declared_tool_refs),
                "memory_refs": list(config.memory_refs),
            },
        )

    def _normalize_input(
        self,
        config: AgentConfig,
        input_payload: BaseModel | Mapping[str, Any],
    ) -> BaseModel:
        """Convert the input payload into the agent's expected input model."""
        if isinstance(input_payload, BaseModel):
            return config.input_model.model_validate(input_payload.model_dump(mode="json"))
        return config.input_model.model_validate(input_payload)


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
        """Combine prompt, response, and extra info into one redacted audit record."""
        record = {
            "prompt": self.serialize_prompt(prompt),
            "response": self.serialize_response(response),
            "extra": self._redact_structure(extra),
        }
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
