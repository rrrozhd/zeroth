"""Capture AutoGen AgentChat model calls as physical charges on the active run.

AgentChat has no global hook, so the recipe wraps each agent's model client::

    planner = AssistantAgent("planner", model_client=ZerothChatCompletionClient(
        OpenAIChatCompletionClient(model="gpt-4.1-mini"),
        model="gpt-4.1-mini", provider="openai", step="planner"))

``create`` and ``create_stream`` charge one physical call each from
``CreateResult.usage`` (prompt and completion tokens; AgentChat reports no cache
split), named by ``step``; a call that raises or a stream closed before its
final result is an unmeasured attempt; a cached result (``CreateResult.cached``)
is not a physical call and records nothing. Everything else is delegated to the
wrapped client unchanged. Identity is declared, never inferred: ``model`` and
``provider`` are what the wrapped client is configured to bill.

This adapter supports ``autogen-agentchat`` 0.7.x (``autogen_core.models``).
AutoGen 0.2 / AG2 (the ``autogen`` / ``pyautogen`` packages) is a separate,
unsupported row: importing this module in such an environment raises a
diagnostic instead of capturing anything.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import AsyncGenerator, Sequence
from time import perf_counter
from typing import Any


def _ag2_present() -> bool:
    if sys.modules.get("autogen") is not None:
        return True
    try:
        return importlib.util.find_spec("autogen") is not None
    except ValueError:
        return False


try:
    from autogen_core.models import ChatCompletionClient, CreateResult, RequestUsage
except ImportError as error:
    if _ag2_present():
        raise ImportError(
            "zeroth.instrumentation.autogen supports autogen-agentchat 0.7.x "
            "(autogen_core.models.ChatCompletionClient). The installed 'autogen' package is "
            "AutoGen 0.2 / AG2, a separate unsupported row: install autogen-agentchat and "
            "autogen-ext, or use the explicit SDK events (C07) for that runtime."
        ) from error
    raise

from zeroth.instrumentation.capture import Run, Usage, current_run  # noqa: E402

PRICED_PROVIDERS = frozenset({"openai", "anthropic"})


def _usage(usage: RequestUsage | None) -> Usage | None:
    if usage is None or (not usage.prompt_tokens and not usage.completion_tokens):
        return None
    return Usage(int(usage.prompt_tokens), int(usage.completion_tokens))


class ZerothChatCompletionClient(ChatCompletionClient):
    """A ChatCompletionClient that charges each physical call of the client it wraps."""

    def __init__(
        self,
        inner: ChatCompletionClient,
        *,
        model: str,
        provider: str,
        step: str | None = None,
        run: Run | None = None,
        priced_providers=PRICED_PROVIDERS,
    ) -> None:
        self._inner = inner
        self._model, self._provider, self._step = model, provider, step
        self._run = run
        self._priced = provider in priced_providers

    @property
    def run(self) -> Run | None:
        return self._run or current_run()

    def _charge(
        self,
        run: Run,
        step: str,
        started: float,
        result: CreateResult | None,
        error: BaseException | None,
        stream: str | None,
    ) -> None:
        if result is not None and getattr(result, "cached", False):
            return  # served from AutoGen's cache: no provider call happened
        run.charge(
            step,
            model=self._model,
            usage=None if result is None else _usage(result.usage),
            provider=self._provider,
            priced=self._priced,
            error=type(error).__name__
            if error is not None
            else ("aborted" if result is None and stream else None),
            latency_ms=int((perf_counter() - started) * 1000),
            # RequestUsage has no cache split: cached prompt tokens are priced as input
            # (OpenAI-style inclusive counts) or dropped (Anthropic-style exclusive counts).
            metadata={
                "framework": "autogen-agentchat",
                "stream": stream,
                "cache_split": "unavailable",
            },
        )

    async def create(self, messages: Sequence[Any], **kwargs: Any) -> CreateResult:
        run = self.run
        if run is None:
            return await self._inner.create(messages, **kwargs)
        step, started = run.next_step(self._step or "model"), perf_counter()
        try:
            result = await self._inner.create(messages, **kwargs)
        except BaseException as error:
            self._charge(run, step, started, None, error, None)
            raise
        self._charge(run, step, started, result, None, None)
        return result

    async def create_stream(
        self, messages: Sequence[Any], **kwargs: Any
    ) -> AsyncGenerator[Any, None]:
        run = self.run
        if run is None:
            async for item in self._inner.create_stream(messages, **kwargs):
                yield item
            return
        step, started = run.next_step(self._step or "model"), perf_counter()
        result: CreateResult | None = None
        try:
            async for item in self._inner.create_stream(messages, **kwargs):
                if isinstance(item, CreateResult):
                    result = item
                yield item
        except GeneratorExit:
            self._charge(
                run, step, started, result, None, "aborted" if result is None else "complete"
            )
            raise
        except BaseException as error:
            self._charge(run, step, started, None, error, "aborted")
            raise
        self._charge(
            run, step, started, result, None, "complete" if result is not None else "aborted"
        )

    def actual_usage(self) -> RequestUsage:
        return self._inner.actual_usage()

    def total_usage(self) -> RequestUsage:
        return self._inner.total_usage()

    def count_tokens(self, messages: Sequence[Any], **kwargs: Any) -> int:
        return self._inner.count_tokens(messages, **kwargs)

    def remaining_tokens(self, messages: Sequence[Any], **kwargs: Any) -> int:
        return self._inner.remaining_tokens(messages, **kwargs)

    @property
    def model_info(self):  # noqa: D102 - delegated
        return self._inner.model_info

    @property
    def capabilities(self):  # noqa: D102 - delegated
        return self._inner.capabilities

    async def close(self) -> None:
        await self._inner.close()


def instrument_autogen(inner: ChatCompletionClient, **options: Any) -> ZerothChatCompletionClient:
    """Wrap a model client; ``model`` and ``provider`` declare who bills the calls."""
    return ZerothChatCompletionClient(inner, **options)
