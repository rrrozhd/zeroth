"""The Zeroth governance callback handler: causal ancestry capture (ZER-3).

Reconstructs the causal execution tree of a governed run purely from LangChain /
LangGraph callback ``run_id`` / ``parent_run_id`` pairs. It **captures** neutral
:class:`~zeroth.integrations.langgraph._spans.CausalSpan` records into an
in-memory sink; it does not deliver, persist (ZER-5) or map them to OpenTelemetry
(ZER-4), and mints no attestation or governance level.

A single instance is shared across all concurrent runs of a governed graph, so
all state is guarded by one lock and keyed by full ``str(run_id)`` -- UUIDv7 ids
share long prefixes and are never truncated. Callback methods are side-effect
only and never raise, so wrapping never changes a graph's results.

Only ``langchain_core`` and the standard library are imported here; ``langgraph``
is never imported, so the package still imports with ``langgraph`` absent.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler

from zeroth.integrations.langgraph._correlation import CORRELATION_METADATA_KEY
from zeroth.integrations.langgraph._spans import CausalSpan, SpanKind

if TYPE_CHECKING:
    # Type-only imports (never evaluated at runtime under ``from __future__ import
    # annotations``), so they cost nothing at import and never pull in langgraph.
    from langchain_core.messages import BaseMessage
    from langchain_core.outputs import ChatGenerationChunk, GenerationChunk, LLMResult

_METADATA_WHITELIST = ("langgraph_node", "langgraph_step", "thread_id")
"""Structural keys copied into a span. No inputs / outputs / free-form metadata."""


def _resolve_span_name(name: Any, serialized: Any, metadata: dict[str, Any] | None) -> str | None:
    """Resolve a span's name from the richest identity source LangChain surfaces.

    Preference order: the explicit callback ``name`` kwarg (a named runnable or
    chain -- e.g. a nested ``RunnableLambda(name=...)``), then the serialized
    runnable / tool / model ``name`` (how tools and chat models carry identity),
    then the LangGraph node name in ``metadata``. Non-string candidates are
    skipped; ``None`` means an anonymous run. Preferring the callback name fixes a
    nested sub-runnable being mislabelled as its enclosing node and a tool / LLM
    span being left nameless.
    """
    if isinstance(name, str) and name:
        return name
    if isinstance(serialized, dict):
        serialized_name = serialized.get("name")
        if isinstance(serialized_name, str) and serialized_name:
            return serialized_name
    node = (metadata or {}).get("langgraph_node")
    return node if isinstance(node, str) else None


class ZerothGovernanceCallbackHandler(BaseCallbackHandler):
    """Capture the causal ``run_id`` ancestry tree of a governed LangGraph run.

    Semantics (all keyed by full ``str(run_id)``, all under one lock):

    * **Start** builds a ``running`` span recording ``parent_run_id``, ``kind``,
      the node name, tags, whitelisted metadata and the correlation id. The
      parent reference is stored verbatim; no root/orphan judgment is made here.
    * **Root vs orphan** -- a determination made when spans are *read*, not at the
      start callback. A span whose ``parent_run_id`` is ``None`` is a tree root
      (always, never an orphan). A span whose ``parent_run_id`` is *not* ``None``
      but names a run id no start was ever observed for is an ``orphan`` (a
      dangling reference): its status is overridden to ``orphan`` in the
      accessors and its parent is never reparented. Deferring to read time is
      what makes out-of-order delivery correct -- a child whose start precedes
      its parent's still resolves to the real parent, present by read time. Many
      roots may coexist on one shared handler, so concurrent and sequential
      top-level runs all classify cleanly.
    * **End / error** move the open span to the completed sink with lifecycle
      status ``ok`` / ``error`` (``error_type`` recorded); exactly one terminal
      per run id -- later end / error events for it are ignored.
    * **Dedup** -- a second start for a run id already seen creates no second
      span.
    * Ancestry comes only from callbacks; out-of-order events are tolerated and no
      ordering is assumed.

    Completed spans are observable via :attr:`completed_spans`, still-open spans
    via :attr:`open_spans`; both apply the read-time orphan determination. An
    optional ``on_span_complete`` callback fires (outside the lock) as each span
    terminates, observing the *lifecycle* status (``ok`` / ``error``): orphan is a
    read-time property of the whole tree, so a per-span terminal notification --
    fired before the observed set is final -- never carries it.
    """

    def __init__(self, *, on_span_complete: Callable[[CausalSpan], None] | None = None) -> None:
        """Create an empty handler, optionally observing each completed span.

        Args:
            on_span_complete: Called with each span as it reaches a terminal
                state, after the internal sink is updated and outside the lock.
                Defaults to ``None`` (spans are only collected into the sink).
        """
        self._lock = threading.Lock()
        self._open: dict[str, CausalSpan] = {}
        self._completed: list[CausalSpan] = []
        self._seen: set[str] = set()
        self._on_span_complete = on_span_complete

    # -- observable sink ---------------------------------------------------

    @property
    def completed_spans(self) -> list[CausalSpan]:
        """A snapshot of terminal spans, dangling-parent ones marked ``orphan``."""
        with self._lock:
            return [self._resolve(span) for span in self._completed]

    @property
    def open_spans(self) -> list[CausalSpan]:
        """A snapshot of still-open spans, dangling-parent ones marked ``orphan``."""
        with self._lock:
            return [self._resolve(span) for span in self._open.values()]

    # -- shared span machinery --------------------------------------------

    def _record_start(
        self,
        kind: SpanKind,
        run_id: UUID,
        parent_run_id: UUID | None,
        tags: list[str] | None,
        metadata: dict[str, Any] | None,
        name: str | None,
    ) -> None:
        """Register a ``running`` span for a start, deduplicating by run id.

        ``name`` is the already-resolved span name (see :func:`_resolve_span_name`).
        The ``parent_run_id`` is stored verbatim; whether that reference is
        dangling (an ``orphan``) is decided at read time by :meth:`_resolve`, not
        here -- so an out-of-order start whose parent has not yet arrived is not
        frozen as an orphan.
        """
        rid = str(run_id)
        prid = str(parent_run_id) if parent_run_id is not None else None
        meta = metadata or {}
        with self._lock:
            if rid in self._seen:
                return
            self._seen.add(rid)
            self._open[rid] = CausalSpan(
                run_id=rid,
                parent_run_id=prid,
                kind=kind,
                name=name,
                start=time.perf_counter(),
                end=None,
                status="running",
                tags=tuple(tags or ()),
                metadata={k: meta[k] for k in _METADATA_WHITELIST if k in meta},
                correlation_id=meta.get(CORRELATION_METADATA_KEY),
                error_type=None,
            )

    def _resolve(self, span: CausalSpan) -> CausalSpan:
        """Return ``span`` with an ``orphan`` status if its parent is dangling.

        A span is an orphan when its ``parent_run_id`` is not ``None`` yet names a
        run id that was never observed (checked against every observed run id).
        Orphan overrides the lifecycle status and never rewrites ``parent_run_id``
        (the dangling id is preserved, never reparented); roots (``parent_run_id
        is None``) are never orphans. Must hold the lock.
        """
        if span.parent_run_id is not None and span.parent_run_id not in self._seen:
            return replace(span, status="orphan")
        return span

    def _record_end(
        self, run_id: UUID, terminal_status: str, error_type: str | None = None
    ) -> None:
        """Move an open span to the completed sink exactly once.

        The span keeps its lifecycle ``terminal_status`` (``ok`` / ``error``); a
        dangling-parent span is surfaced as ``orphan`` only at read time (see
        :meth:`_resolve`), so the completion callback observes the lifecycle
        status, never ``orphan``.
        """
        rid = str(run_id)
        completed: CausalSpan | None = None
        with self._lock:
            span = self._open.pop(rid, None)
            if span is None:
                return
            completed = replace(
                span, end=time.perf_counter(), status=terminal_status, error_type=error_type
            )
            self._completed.append(completed)
        if self._on_span_complete is not None:
            self._on_span_complete(completed)

    # -- start callbacks ---------------------------------------------------

    def on_chain_start(
        self,
        serialized: dict[str, Any],
        inputs: dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Record the start of a chain run (a graph or node)."""
        name = _resolve_span_name(kwargs.get("name"), serialized, metadata)
        self._record_start("chain", run_id, parent_run_id, tags, metadata, name)

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Record the start of a tool run."""
        name = _resolve_span_name(kwargs.get("name"), serialized, metadata)
        self._record_start("tool", run_id, parent_run_id, tags, metadata, name)

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Record the start of a (non-chat) LLM run."""
        name = _resolve_span_name(kwargs.get("name"), serialized, metadata)
        self._record_start("llm", run_id, parent_run_id, tags, metadata, name)

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Record the start of a chat-model run.

        ``serialized`` and ``messages`` are declared as explicit positionals: a
        ``*args`` override would make LangChain fall back to ``on_llm_start`` and
        raise ``IndexError`` when converting messages to prompt strings.
        """
        name = _resolve_span_name(kwargs.get("name"), serialized, metadata)
        self._record_start("chat_model", run_id, parent_run_id, tags, metadata, name)

    def on_llm_new_token(
        self,
        token: str | list[str | dict[str, Any]],
        *,
        chunk: GenerationChunk | ChatGenerationChunk | None = None,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        """Ignore streamed tokens: they carry no ancestry (content is ZER-5)."""

    # -- end / error callbacks --------------------------------------------

    def on_chain_end(
        self,
        outputs: dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Terminate a chain span with ``ok``."""
        self._record_end(run_id, "ok")

    def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Terminate a chain span with ``error``."""
        self._record_end(run_id, "error", type(error).__name__)

    def on_tool_end(
        self, output: Any, *, run_id: UUID, parent_run_id: UUID | None = None, **kwargs: Any
    ) -> None:
        """Terminate a tool span with ``ok``."""
        self._record_end(run_id, "ok")

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Terminate a tool span with ``error``."""
        self._record_end(run_id, "error", type(error).__name__)

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        """Terminate an LLM / chat-model span with ``ok``."""
        self._record_end(run_id, "ok")

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        """Terminate an LLM / chat-model span with ``error``."""
        self._record_end(run_id, "error", type(error).__name__)
