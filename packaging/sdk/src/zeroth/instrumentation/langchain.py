"""Capture LangChain and LangGraph model calls as physical charges on the active run.

``ZerothCallbackHandler`` is a LangChain callback handler: pass it in
``config={"callbacks": [handler]}`` (or attach it to a model/graph) and every
chat-model call the framework observes becomes one charge from the message's
``usage_metadata``, with the provider and model taken from the framework's
response metadata. A model call whose underlying HTTP request was already
charged by a provider adapter (``instrument_openai`` on the same client) is
skipped, so wrapping both layers cannot double count. Chains, graphs and nodes
are aggregates: they are never charged here; the application records the run
summary and outcome once through ``Run``.
"""

from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from zeroth.instrumentation._scope import Scope, close_scope, open_scope
from zeroth.instrumentation.capture import Run, Usage, current_run

PRICED_PROVIDERS = frozenset({"openai", "anthropic"})


@dataclass
class _Span:
    step: str
    scope: Scope
    started: float
    model: str | None
    provider: str | None


def _usage(message: Any) -> Usage | None:
    usage = getattr(message, "usage_metadata", None)
    if not usage:
        return None
    read = (usage.get("input_token_details") or {}).get("cache_read") or 0
    write = (usage.get("input_token_details") or {}).get("cache_creation") or 0
    reasoning = (usage.get("output_token_details") or {}).get("reasoning") or 0
    return Usage(
        usage["input_tokens"] - read, usage["output_tokens"], cache_read_tokens=read,
        cache_write_tokens=write, reasoning_tokens=reasoning,
    )


class ZerothCallbackHandler(BaseCallbackHandler):
    """One charge per observed chat-model call, unless a provider adapter charged it."""

    run_inline = True  # keep the physical scope in the caller's context, also under asyncio

    def __init__(
        self,
        run: Run | None = None,
        *,
        priced_providers: frozenset[str] = PRICED_PROVIDERS,
    ) -> None:
        self._run = run
        self._priced = priced_providers
        self._spans: dict[UUID, _Span] = {}

    @property
    def run(self) -> Run | None:
        return self._run or current_run()

    def _start(self, run_id: UUID, metadata: dict[str, Any] | None) -> None:
        run = self.run
        if run is None:
            return
        metadata = metadata or {}
        step = run.next_step(metadata.get("langgraph_node") or "chat_model")
        self._spans[run_id] = _Span(
            step=step, scope=open_scope(step), started=perf_counter(),
            model=metadata.get("ls_model_name"), provider=metadata.get("ls_provider"),
        )

    def on_chat_model_start(self, serialized, messages, *, run_id, metadata=None, **kwargs):
        self._start(run_id, metadata)

    def on_llm_start(self, serialized, prompts, *, run_id, metadata=None, **kwargs):
        self._start(run_id, metadata)

    def on_llm_end(self, response, *, run_id, **kwargs):
        span = self._spans.pop(run_id, None)
        run = self.run
        if span is None:
            return
        close_scope(span.scope)
        if run is None or span.scope.charged:
            return
        generation = response.generations[0][0] if response.generations else None
        message = getattr(generation, "message", None)
        response_metadata = getattr(message, "response_metadata", None) or {}
        provider = response_metadata.get("model_provider") or span.provider or "unknown"
        model = response_metadata.get("model_name") or span.model or "unknown"
        run.charge(
            span.step, model=model, usage=_usage(message), provider=provider,
            priced=provider in self._priced, request_id=response_metadata.get("id"),
            latency_ms=int((perf_counter() - span.started) * 1000),
            metadata={"framework": "langchain"},
        )

    def on_llm_error(self, error, *, run_id, **kwargs):
        span = self._spans.pop(run_id, None)
        run = self.run
        if span is None:
            return
        close_scope(span.scope)
        if run is None or span.scope.charged:
            return
        provider = span.provider or "unknown"
        run.charge(
            span.step, model=span.model or "unknown", usage=None, provider=provider,
            priced=provider in self._priced, error=type(error).__name__,
            latency_ms=int((perf_counter() - span.started) * 1000),
            metadata={"framework": "langchain"},
        )
