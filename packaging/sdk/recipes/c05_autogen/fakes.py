"""Deterministic AgentChat model clients: queued replies with explicit usage, no network."""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncGenerator, Sequence
from typing import Any

from autogen_core.models import ChatCompletionClient, CreateResult, RequestUsage

INFO = {
    "vision": False,
    "function_calling": False,
    "json_output": False,
    "family": "unknown",
    "structured_output": False,
    "multiple_system_messages": True,
}


def reply(text: str, tokens: list | None, *, cached: bool = False) -> CreateResult:
    usage = (
        RequestUsage(prompt_tokens=0, completion_tokens=0)
        if tokens is None
        else RequestUsage(
            prompt_tokens=tokens[0] + (tokens[2] if len(tokens) > 2 else 0),
            completion_tokens=tokens[1],
        )
    )
    return CreateResult(content=text, finish_reason="stop", usage=usage, cached=cached)


class FakeClient(ChatCompletionClient):
    """Answers from a queue; an Exception item is raised as a failed call."""

    def __init__(self, model: str) -> None:
        self.model, self.queue = model, deque()
        self.calls = 0

    def expect(self, *items: CreateResult | Exception) -> None:
        self.queue.extend(items)

    def _next(self) -> CreateResult:
        self.calls += 1
        item = self.queue.popleft()
        if isinstance(item, Exception):
            raise item
        return item

    async def create(self, messages: Sequence[Any], **kwargs: Any) -> CreateResult:
        token = kwargs.get("cancellation_token")
        if token is not None and token.is_cancelled():
            raise RuntimeError("cancelled before the call")
        return self._next()

    async def create_stream(
        self, messages: Sequence[Any], **kwargs: Any
    ) -> AsyncGenerator[Any, None]:
        result = self._next()
        text = str(result.content)
        yield text[: len(text) // 2]
        yield text[len(text) // 2 :]
        yield result

    def actual_usage(self) -> RequestUsage:
        return RequestUsage(prompt_tokens=0, completion_tokens=0)

    def total_usage(self) -> RequestUsage:
        return RequestUsage(prompt_tokens=0, completion_tokens=0)

    def count_tokens(self, messages: Sequence[Any], **kwargs: Any) -> int:
        return 0

    def remaining_tokens(self, messages: Sequence[Any], **kwargs: Any) -> int:
        return 100_000

    @property
    def model_info(self):  # noqa: D102 - AgentChat protocol
        return INFO

    @property
    def capabilities(self):  # noqa: D102 - AgentChat protocol
        return INFO

    async def close(self) -> None:
        return None


class Clients:
    """One fake client per model name."""

    def __init__(self) -> None:
        self.by_model: dict[str, FakeClient] = {}

    def fake(self, model: str) -> FakeClient:
        return self.by_model.setdefault(model, FakeClient(model))

    @property
    def pending(self) -> int:
        return sum(len(c.queue) for c in self.by_model.values())
