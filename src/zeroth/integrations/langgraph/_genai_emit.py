"""Emit mapped causal spans as a real OpenTelemetry span tree (ZER-4).

The mapping itself lives in :mod:`zeroth.integrations.langgraph._genai`, which
stays free of OpenTelemetry; this module is the only place that touches the SDK,
which is why the package exports :func:`emit_genai_spans` lazily -- importing
``zeroth.integrations.langgraph`` must not require the optional ``otel`` extra.

Feed it ``ZerothGovernanceCallbackHandler.completed_spans``: that batch accessor
is the only path where the read-time orphan determination has run, so a span's
``parent_run_id`` is either resolvable inside the batch or genuinely dangling.
The per-span ``on_span_complete`` hook is *not* a valid input -- it fires mid-run
and by design never carries orphan or tree state.

Ancestry is rebuilt explicitly rather than by nesting context managers: each
child is started with its parent's context via
``opentelemetry.trace.set_span_in_context``, so the exported tree matches the
causal tree even though spans are created and ended one at a time. Ordering is
computed in one iterative pass, so a deep or reverse-ordered batch cannot raise
``RecursionError``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode, Tracer

from zeroth.integrations.langgraph._genai import (
    MappedGenAiSpan,
    PerfCounterAnchor,
    map_causal_span,
)
from zeroth.integrations.langgraph._spans import CausalSpan

_TRACER_NAME = "zeroth.integrations.langgraph.genai"

_STATUS_CODES = {
    "UNSET": StatusCode.UNSET,
    "OK": StatusCode.OK,
    "ERROR": StatusCode.ERROR,
}

_Pair = tuple[CausalSpan, MappedGenAiSpan]


def _index(pairs: tuple[_Pair, ...]) -> dict[str, _Pair]:
    """Index the batch by run id, rejecting an empty or duplicated one.

    Both are rejected before any span is started: an empty id cannot be a tree
    node, and a duplicate would make ancestry ambiguous and silently drop or
    reparent a subtree.

    Raises:
        ValueError: On an empty or duplicate ``run_id``.
    """
    indexed: dict[str, _Pair] = {}
    for pair in pairs:
        run_id = pair[1].run_id
        if not run_id:
            raise ValueError("cannot emit a causal span with an empty run id")
        if run_id in indexed:
            raise ValueError(f"duplicate run id in the emitted batch: {run_id!r}")
        indexed[run_id] = pair
    return indexed


def _emission_order(pairs: tuple[_Pair, ...], indexed: dict[str, _Pair]) -> tuple[str, ...]:
    """Return run ids in parent-before-child order, using one iterative pass.

    A span whose ``parent_run_id`` is ``None`` *or* names a run id absent from
    the batch is treated as a root, so a dangling (orphan) parent still emits.
    Roots and siblings keep their input order, making emission deterministic.
    An explicit stack replaces recursion, so a reverse-ordered chain of any
    depth is fine.

    Any span not reachable from a root is part of a parent cycle (including a
    self-parent); that is detected here, before the first span is started.

    Raises:
        ValueError: If the batch contains a parent cycle.
    """
    children: dict[str, list[str]] = {}
    roots: list[str] = []
    for _, mapped in pairs:
        parent = mapped.parent_run_id
        if parent is None or parent not in indexed:
            roots.append(mapped.run_id)
        else:
            children.setdefault(parent, []).append(mapped.run_id)

    order: list[str] = []
    stack = list(reversed(roots))
    while stack:
        run_id = stack.pop()
        order.append(run_id)
        stack.extend(reversed(children.get(run_id, ())))

    if len(order) != len(indexed):
        cyclic = sorted(set(indexed) - set(order))
        raise ValueError(f"parent cycle in the emitted batch: {cyclic}")
    return tuple(order)


def _start_time_ns(source: CausalSpan, anchor: PerfCounterAnchor | None) -> int | None:
    """Convert the span's ``perf_counter`` start to epoch nanos, or ``None``.

    ``None`` means "let the SDK stamp it": without an anchor the record's
    arbitrary-origin readings cannot be placed on a wall clock, and fabricating
    an epoch time would be worse than an honest emission timestamp.
    """
    if anchor is None:
        return None
    return anchor.epoch_ns + round((source.start - anchor.perf_counter) * 1_000_000_000)


def _end_time_ns(start_ns: int | None, mapped: MappedGenAiSpan) -> int | None:
    """Return the span's absolute end in epoch nanos, or ``None`` to let the SDK stamp it.

    Needs both an anchored start and a known duration: a still-running span has
    no ``end`` reading, so no end time can be derived from it.
    """
    if start_ns is None or mapped.duration_ns is None:
        return None
    return start_ns + mapped.duration_ns


def emit_genai_spans(
    spans: Iterable[CausalSpan],
    *,
    tracer: Tracer | None = None,
    anchor: PerfCounterAnchor | None = None,
) -> tuple[MappedGenAiSpan, ...]:
    """Emit a batch of causal spans as one OpenTelemetry GenAI span tree.

    The whole batch is mapped and validated first, so a rejected batch starts no
    span at all. Root spans are started against the ambient context (nesting the
    causal tree under a caller's span when there is one); every other span is
    started with its in-batch parent's context, so parent/child links and the
    trace id are the real ones.

    Args:
        spans: Causal spans, normally ``handler.completed_spans``. Order is
            irrelevant: ancestry comes from the run ids.
        tracer: Tracer to start spans on. Defaults to
            ``trace.get_tracer("zeroth.integrations.langgraph.genai")``.
        anchor: Reference pair placing the records' ``perf_counter`` readings on
            a wall clock. When ``None`` the SDK stamps emission time and only
            ``MappedGenAiSpan.duration_ns`` is meaningful. A span still running
            has no known duration, so its end is SDK-stamped either way.

    Returns:
        The mapped spans, in emission (parent-before-child) order.

    Raises:
        ValueError: On an empty or duplicate ``run_id``, a parent cycle, or a
            span outside the collection contract. Raised before emission.
    """
    pairs = tuple((source, map_causal_span(source)) for source in spans)
    indexed = _index(pairs)
    order = _emission_order(pairs, indexed)
    emitter = trace.get_tracer(_TRACER_NAME) if tracer is None else tracer

    started: dict[str, Any] = {}
    for run_id in order:
        source, mapped = indexed[run_id]
        parent = started.get(mapped.parent_run_id) if mapped.parent_run_id else None
        context = trace.set_span_in_context(parent) if parent is not None else None
        start_ns = _start_time_ns(source, anchor)
        span = emitter.start_span(
            mapped.name,
            context=context,
            attributes=dict(mapped.attributes),
            start_time=start_ns,
        )
        started[run_id] = span
        # No description: `error_type` is not part of the mapped attribute set,
        # and a status description would be a second, unreviewed text channel.
        span.set_status(Status(_STATUS_CODES[mapped.otel_status_code]))
        span.end(end_time=_end_time_ns(start_ns, mapped))
    return tuple(indexed[run_id][1] for run_id in order)


__all__ = ["emit_genai_spans"]
