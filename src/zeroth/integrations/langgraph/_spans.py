"""The neutral causal span record captured from LangGraph callback ancestry (ZER-3).

This record is deliberately **OpenTelemetry-agnostic**: it carries neutral
``kind`` / ``status`` values and a whitelisted metadata subset, never
``gen_ai.*`` semantic-convention attributes or operation names. Mapping a
:class:`CausalSpan` onto OTel spans is ZER-4; persisting or delivering them is
ZER-5. ZER-3 only *captures* the causal tree.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

SpanKind = Literal["chain", "tool", "llm", "chat_model"]
"""Neutral node kind. NOT an OTel span kind and NOT a ``gen_ai`` operation name."""

SpanStatus = Literal["running", "ok", "error", "orphan"]
"""Neutral lifecycle status.

* ``running`` -- started, not yet terminal.
* ``ok`` -- ended normally.
* ``error`` -- ended via an ``on_*_error`` callback.
* ``orphan`` -- a span whose ``parent_run_id`` names a run id that was never
  observed (a dangling reference). Determined when spans are *read*, not at the
  start callback; it overrides the lifecycle value above and the parent is
  **never** reparented to a root.
"""


@dataclass(frozen=True)
class CausalSpan:
    """One node in the causal execution tree reconstructed from callback run ids.

    Ancestry is expressed solely by ``run_id`` / ``parent_run_id`` (both full
    strings -- UUIDv7 ids share long prefixes, so they are never truncated).
    ``metadata`` holds only a small whitelist of LangGraph structural keys; no
    inputs, outputs, prompts or arbitrary metadata are captured (that is ZER-5).

    Attributes:
        run_id: Full ``str(run_id)`` of this run.
        parent_run_id: Full ``str(parent_run_id)``, or ``None`` for a tree root.
            An orphan keeps its (dangling) parent id verbatim -- only its
            ``status`` changes, never this reference.
        kind: Neutral node kind.
        name: The LangGraph node name (``metadata["langgraph_node"]``) captured
            at start, or ``None``.
        start: ``time.perf_counter()`` reading at start.
        end: ``time.perf_counter()`` reading at the terminal event, or ``None``
            while still running.
        status: Neutral lifecycle status; the handler's accessors override it to
            ``orphan`` at read time for a dangling-parent span (see
            :data:`SpanStatus`).
        tags: Callback ``tags`` captured at start.
        metadata: Whitelisted structural metadata only.
        correlation_id: The UNVERIFIED gateway correlation id carried on the run's
            callback metadata, or ``None``. Unverified because it is extracted
            from the reserved-context token's payload without signature checking
            (see ``_correlation``); enforced mode must never inherit trust from it.
        error_type: ``type(error).__name__`` for an errored span, else ``None``.
    """

    run_id: str
    parent_run_id: str | None
    kind: SpanKind
    name: str | None
    start: float
    end: float | None
    status: SpanStatus
    tags: tuple[str, ...]
    metadata: dict[str, Any]
    correlation_id: str | None
    error_type: str | None


__all__ = ["CausalSpan", "SpanKind", "SpanStatus"]
