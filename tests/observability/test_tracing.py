"""Unit tests for the optional OpenTelemetry tracing layer (OBS)."""

from __future__ import annotations

import asyncio

import pytest

from zeroth.platform.config.settings import TracingSettings
from zeroth.core.observability import configure_tracing, start_span
from zeroth.core.observability.correlation import set_correlation_id


def test_start_span_is_noop_when_disabled() -> None:
    # Default state: tracing disabled -> the context manager must yield cleanly
    # with no provider configured (zero-overhead no-op, no exceptions).
    with start_span("zeroth.test", {"k": "v"}):
        pass


def test_configure_tracing_disabled_returns_false() -> None:
    assert configure_tracing(TracingSettings(enabled=False)) is False


def test_span_emitted_with_attributes_and_correlation_id(otel_spans) -> None:
    set_correlation_id("corr-123")
    with start_span("zeroth.test", {"zeroth.run_id": "r1", "drop_me": None}):
        pass

    spans = otel_spans.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "zeroth.test"
    assert span.attributes["zeroth.run_id"] == "r1"
    assert span.attributes["zeroth.correlation_id"] == "corr-123"
    assert "drop_me" not in span.attributes  # None-valued attributes are filtered


def test_nested_spans_form_parent_child(otel_spans) -> None:
    with start_span("parent"), start_span("child"):
        pass

    spans = {s.name: s for s in otel_spans.get_finished_spans()}
    assert spans["child"].parent.span_id == spans["parent"].context.span_id
    assert spans["child"].context.trace_id == spans["parent"].context.trace_id


@pytest.mark.asyncio
async def test_context_propagates_across_asyncio_tasks(otel_spans) -> None:
    # This is the exact mechanism fan-out relies on: a span active when
    # asyncio tasks are created (gather/create_task copy the contextvar context)
    # becomes the parent of spans opened inside those tasks (OBS-02).
    async def branch() -> None:
        with start_span("branch"):
            await asyncio.sleep(0)

    with start_span("fanout"):
        await asyncio.gather(branch(), branch())

    spans = otel_spans.get_finished_spans()
    fanout = next(s for s in spans if s.name == "fanout")
    branches = [s for s in spans if s.name == "branch"]
    assert len(branches) == 2
    for branch_span in branches:
        assert branch_span.parent.span_id == fanout.context.span_id
        assert branch_span.context.trace_id == fanout.context.trace_id
