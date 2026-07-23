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
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler

from zeroth.integrations.langgraph._correlation import CORRELATION_METADATA_KEY
from zeroth.integrations.langgraph._spans import CausalSpan, SpanKind

_METADATA_WHITELIST = ("langgraph_node", "langgraph_step", "thread_id")
"""Structural keys copied into a span. No inputs / outputs / free-form metadata."""


class ZerothGovernanceCallbackHandler(BaseCallbackHandler):
    """Capture the causal ``run_id`` ancestry tree of a governed LangGraph run.

    Semantics (all keyed by full ``str(run_id)``, all under one lock):

    * **Start** builds a ``running`` span recording ``parent_run_id``, ``kind``,
      the node name, tags, whitelisted metadata and the correlation id.
    * **Root rule** -- a start whose ``parent_run_id`` is ``None`` is a tree root
      *unless another root is still open*, in which case it is an ``orphan``
      (never reparented). This keeps every genuine top-level run a valid root --
      including sequential and concurrent re-invocations sharing this instance --
      while flagging a detached start injected mid-tree.
    * **End / error** move the open span to the completed sink with status ``ok``
      / ``error`` (``error_type`` recorded); exactly one terminal per run id --
      later end / error events for it are ignored. An ``orphan`` keeps that
      status through its terminal.
    * **Dedup** -- a second start for a run id already seen creates no second
      span.
    * Ancestry comes only from callbacks; out-of-order events are tolerated and no
      ordering is assumed.

    Completed spans are observable via :attr:`completed_spans`; still-open spans
    (including orphans) via :attr:`open_spans`. An optional ``on_span_complete``
    callback is invoked (outside the lock) as each span terminates.
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
        self._open_roots: set[str] = set()
        self._seen: set[str] = set()
        self._on_span_complete = on_span_complete

    # -- observable sink ---------------------------------------------------

    @property
    def completed_spans(self) -> list[CausalSpan]:
        """A snapshot list of spans that have reached a terminal state."""
        with self._lock:
            return list(self._completed)

    @property
    def open_spans(self) -> list[CausalSpan]:
        """A snapshot list of spans still open (running or orphaned, no terminal)."""
        with self._lock:
            return list(self._open.values())

    # -- shared span machinery --------------------------------------------

    def _record_start(
        self,
        kind: SpanKind,
        run_id: UUID,
        parent_run_id: UUID | None,
        tags: list[str] | None,
        metadata: dict[str, Any] | None,
    ) -> None:
        """Register a ``running`` (or ``orphan``) span, deduplicating by run id."""
        rid = str(run_id)
        prid = str(parent_run_id) if parent_run_id is not None else None
        meta = metadata or {}
        with self._lock:
            if rid in self._seen:
                return
            self._seen.add(rid)
            status = self._start_status(prid)
            if status == "running" and prid is None:
                self._open_roots.add(rid)
            self._open[rid] = CausalSpan(
                run_id=rid,
                parent_run_id=prid,
                kind=kind,
                name=meta.get("langgraph_node"),
                start=time.perf_counter(),
                end=None,
                status=status,
                tags=tuple(tags or ()),
                metadata={k: meta[k] for k in _METADATA_WHITELIST if k in meta},
                correlation_id=meta.get(CORRELATION_METADATA_KEY),
                error_type=None,
            )

    def _start_status(self, parent_run_id: str | None) -> str:
        """Classify a start: child, root, or orphan (must hold the lock)."""
        if parent_run_id is not None:
            return "running"
        # A root can only open when no other root is currently open; a second
        # concurrently-open root (or a detached start injected mid-tree) is an
        # orphan and is never reparented.
        return "orphan" if self._open_roots else "running"

    def _record_end(
        self, run_id: UUID, terminal_status: str, error_type: str | None = None
    ) -> None:
        """Move an open span to the completed sink exactly once."""
        rid = str(run_id)
        completed: CausalSpan | None = None
        with self._lock:
            span = self._open.pop(rid, None)
            if span is None:
                return
            self._open_roots.discard(rid)
            # An orphan stays an orphan through its terminal (its ancestry was
            # broken); otherwise it takes the ok/error terminal status.
            final_status = "orphan" if span.status == "orphan" else terminal_status
            completed = replace(
                span, end=time.perf_counter(), status=final_status, error_type=error_type
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
        self._record_start("chain", run_id, parent_run_id, tags, metadata)

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
        self._record_start("tool", run_id, parent_run_id, tags, metadata)

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
        self._record_start("llm", run_id, parent_run_id, tags, metadata)

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
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
        self._record_start("chat_model", run_id, parent_run_id, tags, metadata)

    def on_llm_new_token(
        self,
        token: str | list[str | dict[str, Any]],
        *,
        chunk: Any = None,
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
        self, response: Any, *, run_id: UUID, parent_run_id: UUID | None = None, **kwargs: Any
    ) -> None:
        """Terminate an LLM / chat-model span with ``ok``."""
        self._record_end(run_id, "ok")

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Terminate an LLM / chat-model span with ``error``."""
        self._record_end(run_id, "error", type(error).__name__)
