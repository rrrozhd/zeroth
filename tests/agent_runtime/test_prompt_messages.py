"""Conversation-input rendering: PromptConfig.messages_key in the assembler."""

from __future__ import annotations

from pydantic import BaseModel

from zeroth.core.agent_runtime.models import (
    AgentConfig,
    PromptConfig,
    ToolOutputSafetyConfig,
)
from zeroth.core.agent_runtime.prompt import PromptAssembler


class ChatInput(BaseModel):
    topic: str
    messages: list[dict]


class ChatOutput(BaseModel):
    answer: str


def _config(**overrides) -> AgentConfig:
    defaults = dict(
        name="chat-agent",
        instruction="Answer helpfully.",
        model_name="governai:test",
        input_model=ChatInput,
        output_model=ChatOutput,
        prompt_config=PromptConfig(messages_key="messages"),
    )
    defaults.update(overrides)
    return AgentConfig(**defaults)


_PAYLOAD = {
    "topic": "billing",
    "messages": [
        {"role": "human", "content": "What is my balance?"},
        {"role": "ai", "content": "Let me check."},
        {"role": "tool", "name": "billing_lookup", "content": "balance: 42 EUR"},
        {"role": "human", "content": "And in USD?"},
    ],
}


def test_messages_key_renders_conversation_turns() -> None:
    assembly = PromptAssembler().assemble(_config(), _PAYLOAD)

    roles = [message.role for message in assembly.messages]
    # system + input-payload user message, then the four conversation turns
    # (tool output renders as a provenance-wrapped user turn).
    assert roles == ["system", "user", "user", "assistant", "user", "user"]
    assert assembly.messages[2].content == "What is my balance?"
    assert assembly.messages[3].content == "Let me check."
    assert "balance: 42 EUR" in assembly.messages[4].content
    assert "tool:billing_lookup" in assembly.messages[4].content
    assert assembly.messages[5].content == "And in USD?"


def test_messages_are_lifted_out_of_the_input_block() -> None:
    assembly = PromptAssembler().assemble(_config(), _PAYLOAD)

    # The user-facing input JSON block keeps the rest of the payload but not
    # the conversation, which would otherwise appear twice.
    input_block = assembly.messages[1].content
    assert '"topic": "billing"' in input_block
    assert "What is my balance?" not in input_block
    # Audit metadata keeps the raw list.
    assert len(assembly.metadata["conversation_messages"]) == 4


def test_tool_turns_wrap_as_untrusted_when_safety_enabled() -> None:
    assembly = PromptAssembler().assemble(_config(), _PAYLOAD)
    tool_turn = assembly.messages[4].content
    assert "untrusted data" in tool_turn


def test_tool_turns_render_plain_when_safety_disabled() -> None:
    config = _config(tool_output_safety=ToolOutputSafetyConfig(enabled=False))
    assembly = PromptAssembler().assemble(config, _PAYLOAD)
    tool_turn = assembly.messages[4].content
    assert "untrusted data" not in tool_turn
    assert "balance: 42 EUR" in tool_turn


def test_without_messages_key_payload_stays_in_input_block() -> None:
    config = _config(prompt_config=PromptConfig())
    assembly = PromptAssembler().assemble(config, _PAYLOAD)
    assert [message.role for message in assembly.messages] == ["system", "user"]
    assert "What is my balance?" in assembly.messages[1].content
    assert "conversation_messages" not in assembly.metadata
