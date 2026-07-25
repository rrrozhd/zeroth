"""Shared builders for the ZER-4 GenAI mapping tests.

Depends only on :class:`CausalSpan` -- a plain frozen dataclass importable
without ``langgraph`` -- so these tests run in the base suite. They deliberately
carry no ``langgraph_conformance`` marker and no ``importorskip("langgraph")``:
``pyproject.toml`` deselects that marker, so a marked test would never run.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from zeroth.integrations.langgraph._spans import CausalSpan

CONTENT_SENTINEL = "PROMPT_SECRET_29ab"
"""Content-looking value stuffed into records to prove no content channel exists."""


def causal_span(
    run_id: str,
    *,
    parent: str | None = None,
    kind: str = "chain",
    name: str | None = None,
    start: float = 1000.0,
    end: float | None = 1000.5,
    status: str = "ok",
    tags: Sequence[Any] = (),
    metadata: Mapping[str, Any] | None = None,
    correlation_id: str | None = None,
    error_type: str | None = None,
) -> CausalSpan:
    """Build one causal span with test-friendly defaults."""
    return CausalSpan(
        run_id=run_id,
        parent_run_id=parent,
        kind=kind,  # type: ignore[arg-type]
        name=name,
        start=start,
        end=end,
        status=status,  # type: ignore[arg-type]
        tags=tuple(tags),
        metadata=dict(metadata or {}),
        correlation_id=correlation_id,
        error_type=error_type,
    )


def golden_tree() -> tuple[CausalSpan, ...]:
    """A realistic governed-run tree plus an errored and an orphaned span.

    Root ``chain`` (a whole graph) -> nested ``chain`` (a node) -> a
    ``chat_model`` that errored and a ``tool`` that succeeded, plus a span whose
    parent id was never observed. Timings are fixed ``perf_counter``-style
    readings so ``duration_ns`` is deterministic; no wall clock is involved.
    """
    node_metadata = {"langgraph_node": "planner", "langgraph_step": 2, "thread_id": "thread-7"}
    return (
        causal_span(
            "run-root",
            kind="chain",
            name="governed_graph",
            start=1000.0,
            end=1000.5,
            tags=("zeroth", "graph"),
            metadata={"thread_id": "thread-7"},
            correlation_id="corr-abc",
        ),
        causal_span(
            "run-agent",
            parent="run-root",
            kind="chain",
            name=None,
            start=1000.05,
            end=1000.45,
            metadata=node_metadata,
            correlation_id="corr-abc",
        ),
        causal_span(
            "run-chat",
            parent="run-agent",
            kind="chat_model",
            name="gpt-router",
            start=1000.1,
            end=1000.3,
            status="error",
            error_type="TimeoutError",
            metadata=node_metadata,
            correlation_id="corr-abc",
        ),
        causal_span(
            "run-tool",
            parent="run-agent",
            kind="tool",
            name="search_docs",
            start=1000.31,
            end=1000.44,
            tags=("tool",),
            metadata=node_metadata,
            correlation_id="corr-abc",
        ),
        causal_span(
            "run-orphan",
            parent="run-vanished",
            kind="chain",
            name="detached_node",
            start=1000.6,
            end=1000.7,
            status="orphan",
            correlation_id="corr-abc",
        ),
    )
