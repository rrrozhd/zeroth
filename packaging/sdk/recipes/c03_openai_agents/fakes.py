"""Deterministic Agents SDK models: queued ModelResponses, no network, no keys."""

from __future__ import annotations

import itertools
import json
from collections import deque

from agents import Model, ModelProvider, ModelResponse, Usage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails

_ids = itertools.count(1)
_RESPONSE_FIELDS = getattr(ModelResponse, "__dataclass_fields__", {})


def message(text: str = "ok") -> ResponseOutputMessage:
    return ResponseOutputMessage(
        id=f"msg_{next(_ids)}",
        role="assistant",
        status="completed",
        type="message",
        content=[ResponseOutputText(type="output_text", text=text, annotations=[])],
    )


def tool_call(name: str, arguments: dict) -> ResponseFunctionToolCall:
    return ResponseFunctionToolCall(
        type="function_call",
        call_id=f"call_{next(_ids)}",
        name=name,
        arguments=json.dumps(arguments),
    )


def usage(input_tokens: int, output_tokens: int, cached: int = 0) -> Usage:
    """SDK usage where ``input_tokens`` includes the cached tokens, as providers report it."""
    details = {"cached_tokens": cached}
    if "cache_write_tokens" in InputTokensDetails.model_fields:
        details["cache_write_tokens"] = 0
    return Usage(
        requests=1,
        input_tokens=input_tokens + cached,
        output_tokens=output_tokens,
        total_tokens=input_tokens + cached + output_tokens,
        input_tokens_details=InputTokensDetails(**details),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
    )


def reply(output, tokens: list | None, text: str = "ok") -> tuple:
    items = output if isinstance(output, list) else [message(text)]
    return items, (Usage() if tokens is None else usage(*tokens))


class FakeModel(Model):
    def __init__(self, name: str, provider: str) -> None:
        self.model, self.provider, self.queue = name, provider, deque()

    def expect(self, *items) -> None:
        self.queue.extend(items)

    async def get_response(
        self,
        system_instructions,
        input,
        model_settings,
        tools,
        output_schema,
        handoffs,
        tracing,
        **kwargs,
    ) -> ModelResponse:
        item = self.queue.popleft()
        if isinstance(item, Exception):
            raise item
        output, usage_ = item
        extra = {"request_id": "req-fixture"} if "request_id" in _RESPONSE_FIELDS else {}
        return ModelResponse(output=output, usage=usage_, response_id=f"resp_{next(_ids)}", **extra)

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError("streaming is not part of this recipe's verified modes")

    def get_retry_advice(self, *args, **kwargs):
        return None

    async def close(self) -> None:
        return None


class FakeProvider(ModelProvider):
    def __init__(self) -> None:
        self.models: dict[str, FakeModel] = {}

    def get_model(self, model_name: str | None) -> Model:
        name = model_name or "unknown"
        if name not in self.models:
            provider = "anthropic" if name.startswith("claude") else "openai"
            self.models[name] = FakeModel(name, provider)
        return self.models[name]

    @property
    def pending(self) -> int:
        return sum(len(m.queue) for m in self.models.values())
