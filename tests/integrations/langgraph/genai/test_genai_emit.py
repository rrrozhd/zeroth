"""Exporter-integration coverage for the ZER-4 GenAI emit layer.

Exercises the real OpenTelemetry SDK through the root ``otel_spans`` fixture (an
in-memory exporter behind a ``SimpleSpanProcessor``, so spans flush on end). The
emit layer starts spans on its own tracer via ``trace.get_tracer``, so it does
not depend on ``tracing._TRACING_ENABLED``; the globally installed provider is
what matters.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from opentelemetry.context import Context

from zeroth.integrations.langgraph._genai import (
    GEN_AI_CONVERSATION_ID,
    GEN_AI_TOOL_NAME,
    LANGGRAPH_NODE,
    LANGGRAPH_STEP,
    LANGGRAPH_TAGS,
    PerfCounterAnchor,
    ZEROTH_CORRELATION_ID,
    map_causal_span,
)
from zeroth.integrations.langgraph._genai_emit import emit_genai_spans

from ._causal import BLANKS, CONTENT_SENTINEL, HostileStr, causal_span, golden_tree


class _RecordingSpan:
    """A span double: records nothing, satisfies the emit layer's span protocol."""

    def __init__(self) -> None:
        self.status: Any = None
        self.end_time: int | None = None

    def set_status(self, status: Any) -> None:
        self.status = status

    def end(self, end_time: int | None = None) -> None:
        self.end_time = end_time


class _RecordingTracer:
    """A tracer double capturing every ``start_span`` call, in order."""

    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []

    def start_span(
        self,
        name: str,
        context: Any = None,
        attributes: Any = None,
        start_time: int | None = None,
        **kwargs: Any,
    ) -> _RecordingSpan:
        self.started.append(
            {"name": name, "context": context, "attributes": attributes, "start_time": start_time}
        )
        return _RecordingSpan()


def _by_name(exporter: Any) -> dict[str, Any]:
    spans = exporter.get_finished_spans()
    assert len({span.name for span in spans}) == len(spans), "span names must be unique here"
    return {span.name: span for span in spans}


# -- R5: the real parent/child tree --------------------------------------------


def test_emitted_spans_rebuild_the_causal_tree(otel_spans: Any) -> None:
    mapped = emit_genai_spans(golden_tree())
    exported = _by_name(otel_spans)

    assert [item.run_id for item in mapped] == [
        "run-root",
        "run-agent",
        "run-chat",
        "run-tool",
        "run-orphan",
    ]
    root = exported["invoke_workflow governed_graph"]
    agent = exported["invoke_agent planner"]
    chat = exported["chat gpt-router"]
    tool = exported["execute_tool search_docs"]

    assert root.parent is None
    assert agent.parent.span_id == root.context.span_id
    assert chat.parent.span_id == agent.context.span_id
    assert tool.parent.span_id == agent.context.span_id
    assert {span.context.trace_id for span in (root, agent, chat, tool)} == {root.context.trace_id}


def test_two_independent_roots_detach_from_the_ambient_span(otel_spans: Any) -> None:
    """R5: one trace per causal tree, whatever span happens to be active.

    These records are HISTORICAL -- their ``start`` / ``end`` are past
    ``perf_counter`` readings -- so inheriting the ambient span would misattribute
    a replayed tree to an unrelated caller and make two independent roots siblings
    of the same trace. Roots are therefore started with an empty ``Context``.
    """
    from opentelemetry import trace

    ambient_tracer = trace.get_tracer("test.ambient")
    spans = (
        causal_span("run-a", kind="chain", name="graph-a"),
        causal_span("run-a-child", parent="run-a", kind="tool", name="tool-a"),
        causal_span("run-b", kind="chain", name="graph-b"),
        causal_span("run-b-child", parent="run-b", kind="tool", name="tool-b"),
    )

    with ambient_tracer.start_as_current_span("ambient") as ambient:
        ambient_trace_id = ambient.get_span_context().trace_id
        emit_genai_spans(spans)

    exported = _by_name(otel_spans)
    root_a = exported["invoke_workflow graph-a"]
    root_b = exported["invoke_workflow graph-b"]
    child_a = exported["execute_tool tool-a"]
    child_b = exported["execute_tool tool-b"]

    # (a) neither root is parented, (b) they are two distinct traces, and
    # (c) neither belongs to the ambient span's trace.
    assert root_a.parent is None
    assert root_b.parent is None
    assert root_a.context.trace_id != root_b.context.trace_id
    assert ambient_trace_id not in {root_a.context.trace_id, root_b.context.trace_id}
    # (d) each child sits under its own root, in that root's trace.
    assert child_a.parent.span_id == root_a.context.span_id
    assert child_a.context.trace_id == root_a.context.trace_id
    assert child_b.parent.span_id == root_b.context.span_id
    assert child_b.context.trace_id == root_b.context.trace_id


def test_an_orphan_root_also_detaches_from_the_ambient_span(otel_spans: Any) -> None:
    from opentelemetry import trace

    ambient_tracer = trace.get_tracer("test.ambient")

    with ambient_tracer.start_as_current_span("ambient") as ambient:
        ambient_trace_id = ambient.get_span_context().trace_id
        emit_genai_spans([causal_span("run-orphan", parent="run-vanished", status="orphan")])

    orphan = _by_name(otel_spans)["invoke_agent"]

    assert orphan.parent is None
    assert orphan.context.trace_id != ambient_trace_id
    # The dangling reference is still reported, never reparented away.
    assert orphan.attributes["langgraph.parent_run_id"] == "run-vanished"


def test_a_parent_absent_from_the_batch_is_emitted_as_a_root(otel_spans: Any) -> None:
    emit_genai_spans([causal_span("run-orphan", parent="run-vanished", status="orphan")])
    (exported,) = otel_spans.get_finished_spans()

    assert exported.parent is None
    assert exported.attributes["langgraph.parent_run_id"] == "run-vanished"
    assert exported.attributes["zeroth.span_status"] == "orphan"


def test_exported_attributes_are_exactly_the_mapped_attributes(otel_spans: Any) -> None:
    sources = golden_tree()
    mapped = emit_genai_spans(sources)
    exported = _by_name(otel_spans)

    for item in mapped:
        span = exported[item.name]
        assert dict(span.attributes) == dict(item.attributes)


def test_correlation_id_rides_every_span_that_has_one(otel_spans: Any) -> None:
    emit_genai_spans(golden_tree())
    spans = otel_spans.get_finished_spans()

    assert len(spans) == 5
    assert {span.attributes[ZEROTH_CORRELATION_ID] for span in spans} == {"corr-abc"}


def test_a_span_without_a_correlation_id_omits_the_attribute(otel_spans: Any) -> None:
    emit_genai_spans([causal_span("run-1", correlation_id=None)])
    (exported,) = otel_spans.get_finished_spans()

    assert ZEROTH_CORRELATION_ID not in exported.attributes


def test_error_status_is_exported_without_a_description(otel_spans: Any) -> None:
    from opentelemetry.trace import StatusCode

    emit_genai_spans(
        [causal_span("run-1", status="error", error_type=CONTENT_SENTINEL, name="worker")]
    )
    (exported,) = otel_spans.get_finished_spans()

    assert exported.status.status_code is StatusCode.ERROR
    assert exported.status.description is None
    assert not exported.events


# -- timestamps ----------------------------------------------------------------


def test_an_anchor_derives_absolute_start_and_end_nanos(otel_spans: Any) -> None:
    anchor = PerfCounterAnchor(perf_counter=1000.0, epoch_ns=1_700_000_000_000_000_000)
    spans = (
        causal_span("run-root", kind="chain", name="graph", start=1000.0, end=1000.5),
        causal_span(
            "run-tool", parent="run-root", kind="tool", name="t", start=1000.25, end=1000.5
        ),
    )

    emit_genai_spans(spans, anchor=anchor)
    exported = _by_name(otel_spans)

    root = exported["invoke_workflow graph"]
    tool = exported["execute_tool t"]
    assert root.start_time == 1_700_000_000_000_000_000
    assert root.end_time == 1_700_000_000_500_000_000
    assert tool.start_time == 1_700_000_000_250_000_000
    assert tool.end_time == 1_700_000_000_500_000_000


def test_without_an_anchor_the_sdk_stamps_wall_clock_time(otel_spans: Any) -> None:
    before = time.time_ns()
    emit_genai_spans([causal_span("run-1", name="graph", start=1000.0, end=1000.5)])
    after = time.time_ns()
    (exported,) = otel_spans.get_finished_spans()

    # Never a perf_counter reading reinterpreted as an epoch time.
    assert before <= exported.start_time <= after
    assert before <= exported.end_time <= after


def test_a_running_span_gets_an_sdk_stamped_end_even_with_an_anchor(otel_spans: Any) -> None:
    anchor = PerfCounterAnchor(perf_counter=1000.0, epoch_ns=1_700_000_000_000_000_000)
    before = time.time_ns()

    emit_genai_spans(
        [causal_span("run-1", name="g", start=1000.0, end=None, status="running")], anchor=anchor
    )
    (exported,) = otel_spans.get_finished_spans()

    assert exported.start_time == 1_700_000_000_000_000_000
    assert exported.end_time >= before


# -- R6 / R7 through the exporter ---------------------------------------------


def test_rejected_metadata_produces_no_exported_attribute(otel_spans: Any) -> None:
    class _SneakyStr(str):
        pass

    emit_genai_spans(
        [
            causal_span(
                "run-1",
                name="worker",
                metadata={
                    "langgraph_step": True,
                    "langgraph_node": _SneakyStr("planner"),
                    "thread_id": "thread-7",
                    "prompt": CONTENT_SENTINEL,
                },
            )
        ]
    )
    (exported,) = otel_spans.get_finished_spans()

    assert LANGGRAPH_STEP not in exported.attributes
    assert LANGGRAPH_NODE not in exported.attributes
    assert exported.attributes[GEN_AI_CONVERSATION_ID] == "thread-7"
    assert not [key for key in exported.attributes if "prompt" in key]


def test_no_content_sentinel_reaches_the_exporter(otel_spans: Any) -> None:
    emit_genai_spans(
        [
            causal_span(
                "run-1",
                kind="tool",
                name="search_docs",
                status="error",
                error_type=CONTENT_SENTINEL,
                tags=("safe",),
                metadata={
                    "prompt": CONTENT_SENTINEL,
                    "inputs": CONTENT_SENTINEL,
                    "outputs": CONTENT_SENTINEL,
                    "thread_id": "thread-7",
                },
            )
        ]
    )
    (exported,) = otel_spans.get_finished_spans()

    assert CONTENT_SENTINEL not in exported.name
    assert CONTENT_SENTINEL not in str(exported.status.description)
    assert not exported.events
    for key, value in exported.attributes.items():
        assert CONTENT_SENTINEL not in str(value), key
    assert exported.attributes[GEN_AI_TOOL_NAME] == "search_docs"
    assert exported.attributes[LANGGRAPH_TAGS] == ("safe",)


@pytest.mark.parametrize("blank", BLANKS)
def test_a_blank_string_exports_no_attribute_at_all(blank: str, otel_spans: Any) -> None:
    emit_genai_spans(
        [
            causal_span(
                "run-1",
                kind="tool",
                name=blank,
                correlation_id=blank,
                tags=(blank,),
                metadata={"thread_id": blank, "langgraph_node": blank, "langgraph_step": 0},
            )
        ]
    )
    (exported,) = otel_spans.get_finished_spans()

    assert exported.name == "execute_tool"
    for key in (
        GEN_AI_TOOL_NAME,
        GEN_AI_CONVERSATION_ID,
        ZEROTH_CORRELATION_ID,
        LANGGRAPH_NODE,
        LANGGRAPH_TAGS,
    ):
        assert key not in exported.attributes
    # An int is unaffected: 0 is a real step number, not an absent one.
    assert exported.attributes[LANGGRAPH_STEP] == 0
    assert not [key for key, value in exported.attributes.items() if value == ""]


def test_a_hostile_str_subclass_reaches_no_exported_channel(otel_spans: Any) -> None:
    """The gate is what stops it: OTel's own attribute check is ``isinstance``."""
    hostile = HostileStr("planner")

    emit_genai_spans(
        [
            causal_span(
                "run-1",
                kind="tool",
                name=hostile,
                correlation_id=hostile,
                tags=(hostile, "safe"),
                metadata={"thread_id": hostile, "langgraph_node": hostile},
            )
        ]
    )
    (exported,) = otel_spans.get_finished_spans()

    assert exported.name == "execute_tool"
    assert CONTENT_SENTINEL not in exported.name
    assert CONTENT_SENTINEL not in str(exported.status.description)
    assert not exported.events
    for key, value in exported.attributes.items():
        assert CONTENT_SENTINEL not in str(value), key
        assert type(value) in (str, int, tuple), key
    assert exported.attributes[LANGGRAPH_TAGS] == ("safe",)
    for key in (GEN_AI_TOOL_NAME, GEN_AI_CONVERSATION_ID, ZEROTH_CORRELATION_ID, LANGGRAPH_NODE):
        assert key not in exported.attributes


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", HostileStr("run-1")),
        ("parent", HostileStr("run-0")),
        ("kind", HostileStr("chain")),
        ("status", HostileStr("ok")),
    ],
)
def test_an_untrusted_identity_field_starts_no_span(field: str, value: Any) -> None:
    tracer = _RecordingTracer()
    fields: dict[str, Any] = {"run_id": "run-1", "kind": "chain", "status": "ok", field: value}

    with pytest.raises(ValueError):
        emit_genai_spans([causal_span(**fields)], tracer=tracer)
    assert tracer.started == []


# -- topology validation, before any span is started ---------------------------


def test_a_duplicate_run_id_is_rejected_before_emission() -> None:
    tracer = _RecordingTracer()

    with pytest.raises(ValueError, match="duplicate run id"):
        emit_genai_spans([causal_span("run-1"), causal_span("run-1")], tracer=tracer)
    assert tracer.started == []


def test_an_empty_run_id_is_rejected_before_emission() -> None:
    tracer = _RecordingTracer()

    with pytest.raises(ValueError, match="empty run id"):
        emit_genai_spans([causal_span("")], tracer=tracer)
    assert tracer.started == []


def test_a_two_node_parent_cycle_starts_no_span() -> None:
    tracer = _RecordingTracer()
    spans = (causal_span("run-a", parent="run-b"), causal_span("run-b", parent="run-a"))

    with pytest.raises(ValueError, match="parent cycle"):
        emit_genai_spans(spans, tracer=tracer)
    assert tracer.started == []


def test_a_self_parent_is_a_cycle_and_starts_no_span() -> None:
    tracer = _RecordingTracer()

    with pytest.raises(ValueError, match="parent cycle"):
        emit_genai_spans([causal_span("run-a", parent="run-a")], tracer=tracer)
    assert tracer.started == []


def test_a_cycle_beside_a_valid_root_still_starts_no_span() -> None:
    tracer = _RecordingTracer()
    spans = (
        causal_span("run-root"),
        causal_span("run-a", parent="run-b"),
        causal_span("run-b", parent="run-a"),
    )

    with pytest.raises(ValueError, match="parent cycle"):
        emit_genai_spans(spans, tracer=tracer)
    assert tracer.started == []


def test_a_deep_reverse_ordered_chain_emits_parent_before_child() -> None:
    depth = 1500
    chain = [causal_span("run-0", kind="chain", name="node-0")]
    chain.extend(
        causal_span(f"run-{index}", parent=f"run-{index - 1}", kind="chain", name=f"node-{index}")
        for index in range(1, depth)
    )
    tracer = _RecordingTracer()

    mapped = emit_genai_spans(reversed(chain), tracer=tracer)

    assert [item.run_id for item in mapped] == [f"run-{index}" for index in range(depth)]
    assert [call["name"] for call in tracer.started] == [
        "invoke_workflow node-0",
        *(f"invoke_agent node-{index}" for index in range(1, depth)),
    ]
    # An empty Context() detaches the root; a child always carries its parent's.
    assert tracer.started[0]["context"] == Context()
    assert all(call["context"] != Context() for call in tracer.started[1:])


def test_an_explicit_tracer_receives_every_span() -> None:
    tracer = _RecordingTracer()

    mapped = emit_genai_spans(golden_tree(), tracer=tracer)

    assert len(tracer.started) == len(mapped) == 5
    assert tracer.started[0]["attributes"] == dict(mapped[0].attributes)
    assert tracer.started[0]["start_time"] is None


def test_an_empty_batch_emits_nothing(otel_spans: Any) -> None:
    assert emit_genai_spans([]) == ()
    assert otel_spans.get_finished_spans() == ()
