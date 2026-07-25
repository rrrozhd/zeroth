"""Policy and golden coverage for the ZER-4 causal-span -> GenAI mapper.

Pure-mapping tests: no OpenTelemetry, no langgraph. Exporter behaviour lives in
``test_genai_emit.py``; vendored-constant drift in ``test_semconv_drift.py``.
"""

from __future__ import annotations

import inspect
import itertools
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from zeroth.integrations.langgraph._genai import (
    ATTRIBUTE_NAMESPACES,
    GEN_AI_AGENT_NAME,
    GEN_AI_CONVERSATION_ID,
    GEN_AI_NAMESPACE,
    GEN_AI_OPERATION_NAME,
    GEN_AI_TOOL_NAME,
    GEN_AI_WORKFLOW_NAME,
    GENAI_CONVENTION_VERSION,
    GENAI_OPERATION_NAMES,
    LANGGRAPH_KIND,
    LANGGRAPH_NAMESPACE,
    LANGGRAPH_NODE,
    LANGGRAPH_PARENT_RUN_ID,
    LANGGRAPH_RUN_ID,
    LANGGRAPH_STEP,
    LANGGRAPH_TAGS,
    OPERATION_CHAT,
    OPERATION_EMBEDDINGS,
    OPERATION_EXECUTE_TOOL,
    OPERATION_INVOKE_AGENT,
    OPERATION_INVOKE_WORKFLOW,
    OPERATION_RETRIEVAL,
    UNREACHABLE_OPERATION_NAMES,
    ZEROTH_CONVENTION_VERSION,
    ZEROTH_CORRELATION_ID,
    ZEROTH_NAMESPACE,
    ZEROTH_SPAN_STATUS,
    _OPERATION_BY_KIND,
    map_causal_span,
)

from zeroth.integrations.langgraph._spans import CausalSpan

from ._causal import BLANKS, CONTENT_SENTINEL, HostileStr, causal_span, golden_tree

FIXTURES = Path(__file__).with_name("fixtures")
GOLDEN_TREE = "mapped_genai_tree.json"


def _load(name: str, *, fixtures: Path = FIXTURES) -> Any:
    path = fixtures / name
    if not path.exists():
        raise AssertionError(f"missing golden fixture {path}; regenerate with REGEN_GOLDENS=1")
    return json.loads(path.read_text())


def _golden(name: str, payload: Any, *, fixtures: Path = FIXTURES) -> Any:
    """Return the stored golden, rewriting it first when ``REGEN_GOLDENS=1``."""
    if os.environ.get("REGEN_GOLDENS") == "1":
        fixtures.mkdir(parents=True, exist_ok=True)
        (fixtures / name).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return _load(name, fixtures=fixtures)


# -- R2: the operation-name table ---------------------------------------------


def test_operation_table_is_exhaustive_over_kind_and_root_flag() -> None:
    assert dict(_OPERATION_BY_KIND) == {
        ("tool", True): OPERATION_EXECUTE_TOOL,
        ("tool", False): OPERATION_EXECUTE_TOOL,
        ("llm", True): OPERATION_CHAT,
        ("llm", False): OPERATION_CHAT,
        ("chat_model", True): OPERATION_CHAT,
        ("chat_model", False): OPERATION_CHAT,
        ("chain", True): OPERATION_INVOKE_WORKFLOW,
        ("chain", False): OPERATION_INVOKE_AGENT,
    }


def test_vendored_operation_names_include_the_unreachable_semconv_values() -> None:
    assert {
        OPERATION_CHAT,
        OPERATION_EXECUTE_TOOL,
        OPERATION_INVOKE_AGENT,
        OPERATION_INVOKE_WORKFLOW,
        OPERATION_RETRIEVAL,
        OPERATION_EMBEDDINGS,
    } == GENAI_OPERATION_NAMES
    # Unreachable until collection grows retriever / embeddings callbacks, which
    # would be a collection-contract change (out of scope for ZER-4).
    assert {OPERATION_RETRIEVAL, OPERATION_EMBEDDINGS} == UNREACHABLE_OPERATION_NAMES
    assert UNREACHABLE_OPERATION_NAMES.isdisjoint(set(_OPERATION_BY_KIND.values()))


@pytest.mark.parametrize(
    ("kind", "parent", "operation"),
    [
        ("tool", None, OPERATION_EXECUTE_TOOL),
        ("tool", "run-p", OPERATION_EXECUTE_TOOL),
        ("llm", None, OPERATION_CHAT),
        ("llm", "run-p", OPERATION_CHAT),
        ("chat_model", None, OPERATION_CHAT),
        ("chat_model", "run-p", OPERATION_CHAT),
        ("chain", None, OPERATION_INVOKE_WORKFLOW),
        ("chain", "run-p", OPERATION_INVOKE_AGENT),
    ],
)
def test_every_kind_and_root_flag_maps_through_the_public_mapper(
    kind: str, parent: str | None, operation: str
) -> None:
    mapped = map_causal_span(causal_span("run-1", parent=parent, kind=kind, name="target"))

    assert mapped.operation == operation
    assert mapped.attributes[GEN_AI_OPERATION_NAME] == operation
    assert mapped.name == f"{operation} target"


def test_unmappable_kind_or_status_is_rejected() -> None:
    with pytest.raises(ValueError, match="unmappable causal span"):
        map_causal_span(causal_span("run-1", kind="retriever"))
    with pytest.raises(ValueError, match="unmappable causal span"):
        map_causal_span(causal_span("run-1", status="cancelled"))


# -- R2: span names ------------------------------------------------------------


def test_span_name_prefers_the_record_name_then_the_node_then_the_operation() -> None:
    named = map_causal_span(causal_span("r1", kind="tool", name="search", metadata={}))
    from_node = map_causal_span(
        causal_span("r2", kind="tool", name=None, metadata={"langgraph_node": "lookup"})
    )
    anonymous = map_causal_span(causal_span("r3", kind="tool", name=None, metadata={}))

    assert named.name == "execute_tool search"
    assert from_node.name == "execute_tool lookup"
    assert anonymous.name == "execute_tool"


# -- R3: standard identifiers, present only when available --------------------


def test_target_identifier_follows_the_operation() -> None:
    tool = map_causal_span(causal_span("r1", kind="tool", name="search"))
    agent = map_causal_span(causal_span("r2", parent="r1", kind="chain", name="planner"))
    workflow = map_causal_span(causal_span("r3", kind="chain", name="graph"))
    chat = map_causal_span(causal_span("r4", kind="chat_model", name="gpt-router"))

    assert tool.attributes[GEN_AI_TOOL_NAME] == "search"
    assert agent.attributes[GEN_AI_AGENT_NAME] == "planner"
    assert workflow.attributes[GEN_AI_WORKFLOW_NAME] == "graph"
    # A chat span's semconv subject is gen_ai.request.model / gen_ai.provider.name,
    # neither of which a CausalSpan carries -- so nothing is emitted, not a guess.
    assert {key for key in chat.attributes if key.startswith(GEN_AI_NAMESPACE)} == {
        GEN_AI_OPERATION_NAME
    }


def test_absent_identifiers_are_omitted_entirely_never_null_or_empty() -> None:
    mapped = map_causal_span(causal_span("r1", kind="tool", name=None, metadata={}, tags=()))

    assert GEN_AI_TOOL_NAME not in mapped.attributes
    assert GEN_AI_CONVERSATION_ID not in mapped.attributes
    assert LANGGRAPH_PARENT_RUN_ID not in mapped.attributes
    assert LANGGRAPH_TAGS not in mapped.attributes
    assert ZEROTH_CORRELATION_ID not in mapped.attributes
    assert all(value not in (None, "", ()) for value in mapped.attributes.values())


def test_conversation_id_comes_from_the_thread_id() -> None:
    mapped = map_causal_span(causal_span("r1", metadata={"thread_id": "thread-7"}))

    assert mapped.attributes[GEN_AI_CONVERSATION_ID] == "thread-7"


# -- R4: three disjoint namespaces --------------------------------------------


def _fully_populated() -> Any:
    return map_causal_span(
        causal_span(
            "run-child",
            parent="run-parent",
            kind="tool",
            name="search_docs",
            tags=("alpha", "beta"),
            metadata={"langgraph_node": "planner", "langgraph_step": 3, "thread_id": "thread-7"},
            correlation_id="corr-abc",
            status="error",
            error_type="TimeoutError",
        )
    )


def test_attribute_set_is_exactly_the_three_declared_namespaces() -> None:
    attributes = _fully_populated().attributes

    assert set(attributes) == {
        GEN_AI_OPERATION_NAME,
        GEN_AI_TOOL_NAME,
        GEN_AI_CONVERSATION_ID,
        LANGGRAPH_RUN_ID,
        LANGGRAPH_PARENT_RUN_ID,
        LANGGRAPH_KIND,
        LANGGRAPH_NODE,
        LANGGRAPH_STEP,
        LANGGRAPH_TAGS,
        ZEROTH_CORRELATION_ID,
        ZEROTH_CONVENTION_VERSION,
        ZEROTH_SPAN_STATUS,
    }
    assert all(key.startswith(ATTRIBUTE_NAMESPACES) for key in attributes)


def test_the_three_namespaces_partition_the_attribute_keys() -> None:
    attributes = _fully_populated().attributes
    groups = [
        {key for key in attributes if key.startswith(namespace)}
        for namespace in ATTRIBUTE_NAMESPACES
    ]

    assert set().union(*groups) == set(attributes)
    for left, right in itertools.combinations(groups, 2):
        assert not left & right
    # No namespace is a prefix of another, so the partition cannot degenerate.
    for left, right in itertools.permutations(ATTRIBUTE_NAMESPACES, 2):
        assert not left.startswith(right)


def test_governance_attributes_never_leak_into_the_gen_ai_namespace() -> None:
    attributes = _fully_populated().attributes

    assert {key for key in attributes if key.startswith(GEN_AI_NAMESPACE)} == {
        GEN_AI_OPERATION_NAME,
        GEN_AI_TOOL_NAME,
        GEN_AI_CONVERSATION_ID,
    }
    # The correlation id is UNVERIFIED gateway state: it stays under zeroth.*,
    # where no consumer can mistake it for a trusted semconv identifier.
    assert attributes[ZEROTH_CORRELATION_ID] == "corr-abc"
    assert "corr-abc" not in {
        value for key, value in attributes.items() if key.startswith(GEN_AI_NAMESPACE)
    }
    assert attributes[ZEROTH_CONVENTION_VERSION] == GENAI_CONVENTION_VERSION
    assert attributes[ZEROTH_SPAN_STATUS] == "error"


def test_langgraph_namespace_carries_the_ancestry_and_structure() -> None:
    attributes = _fully_populated().attributes

    assert {key: attributes[key] for key in attributes if key.startswith(LANGGRAPH_NAMESPACE)} == {
        LANGGRAPH_RUN_ID: "run-child",
        LANGGRAPH_PARENT_RUN_ID: "run-parent",
        LANGGRAPH_KIND: "tool",
        LANGGRAPH_NODE: "planner",
        LANGGRAPH_STEP: 3,
        LANGGRAPH_TAGS: ("alpha", "beta"),
    }


def test_orphan_status_survives_verbatim_without_being_reported_as_an_error() -> None:
    mapped = map_causal_span(causal_span("r1", parent="gone", status="orphan"))

    assert mapped.span_status == "orphan"
    assert mapped.attributes[ZEROTH_SPAN_STATUS] == "orphan"
    assert mapped.otel_status_code == "UNSET"
    # The dangling reference is preserved, never reparented to a root.
    assert mapped.parent_run_id == "gone"
    assert mapped.attributes[LANGGRAPH_PARENT_RUN_ID] == "gone"


@pytest.mark.parametrize(
    ("status", "code"),
    [("running", "UNSET"), ("ok", "OK"), ("error", "ERROR"), ("orphan", "UNSET")],
)
def test_every_neutral_status_maps_to_an_otel_status_code(status: str, code: str) -> None:
    mapped = map_causal_span(causal_span("r1", parent="p", status=status))

    assert mapped.otel_status_code == code
    assert mapped.attributes[ZEROTH_SPAN_STATUS] == status


# -- R6: allowlisted metadata with exact-type gates ---------------------------


class _SneakyStr(str):
    """A ``str`` subclass: passes ``isinstance``, must fail the exact-type gate."""


def test_only_allowlisted_keys_with_exact_scalar_types_map() -> None:
    mapped = map_causal_span(
        causal_span(
            "r1",
            kind="tool",
            name="search",
            metadata={
                "langgraph_node": "planner",
                "langgraph_step": 4,
                "thread_id": "thread-7",
                "prompt": "unwanted",
                "inputs": "unwanted",
                "user_id": 99,
            },
        )
    )

    assert mapped.attributes[LANGGRAPH_NODE] == "planner"
    assert mapped.attributes[LANGGRAPH_STEP] == 4
    assert mapped.attributes[GEN_AI_CONVERSATION_ID] == "thread-7"
    assert not [key for key in mapped.attributes if "prompt" in key or "input" in key]
    assert 99 not in set(mapped.attributes.values())


def test_bool_is_rejected_even_though_it_is_an_int_subclass() -> None:
    # Collection's own isinstance filter keeps bool, so the gate has to be here.
    mapped = map_causal_span(causal_span("r1", metadata={"langgraph_step": True}))

    assert LANGGRAPH_STEP not in mapped.attributes


def test_str_subclass_is_rejected_and_does_not_become_a_target() -> None:
    mapped = map_causal_span(
        causal_span(
            "r1",
            kind="tool",
            name=None,
            metadata={"langgraph_node": _SneakyStr("planner"), "thread_id": _SneakyStr("t")},
        )
    )

    assert LANGGRAPH_NODE not in mapped.attributes
    assert GEN_AI_CONVERSATION_ID not in mapped.attributes
    assert GEN_AI_TOOL_NAME not in mapped.attributes
    assert mapped.name == OPERATION_EXECUTE_TOOL


def test_non_string_tags_are_dropped_so_the_sequence_stays_homogeneous() -> None:
    mapped = map_causal_span(causal_span("r1", tags=("keep", 7, None, _SneakyStr("drop"))))

    assert mapped.attributes[LANGGRAPH_TAGS] == ("keep",)


class _SneakyTags(tuple):  # noqa: SLOT001 - the point is to subclass tuple
    """A ``tuple`` subclass whose ``__iter__`` yields entries it never stored."""

    def __iter__(self):  # type: ignore[override]
        return iter(("injected",))


def test_a_hostile_tag_container_is_omitted_not_iterated() -> None:
    """Per-entry gates cannot help if the container itself lies.

    A ``tuple`` subclass may override ``__iter__`` to yield entries it never
    stored, so the container is gated on ``type(...) is tuple`` and anything
    else is omitted entirely. ``CausalSpan.__post_init__`` normalises only
    ``metadata``, so such a container really does survive to the mapper --
    hence the span is built directly here rather than through
    :func:`causal_span`, whose ``tuple(tags)`` would materialise the lie first.
    """
    span = CausalSpan(
        run_id="r1",
        parent_run_id=None,
        kind="chain",
        name=None,
        start=1000.0,
        end=1000.5,
        status="ok",
        tags=_SneakyTags(("safe",)),
        metadata={},
        correlation_id=None,
        error_type=None,
    )
    assert type(span.tags) is not tuple  # the subclass reaches the mapper intact

    mapped = map_causal_span(span)

    assert LANGGRAPH_TAGS not in mapped.attributes
    assert "injected" not in repr(mapped)
    assert "injected" not in json.dumps(mapped.to_dict())


# -- a blank string is ABSENT, never an empty attribute value ------------------


@pytest.mark.parametrize("blank", BLANKS)
def test_a_blank_string_is_absent_rather_than_an_empty_attribute(blank: str) -> None:
    """Empty and whitespace-only values are omitted, not emitted as ``""``.

    ``gen_ai.conversation.id=""`` would contradict R3 and the "never empty"
    contract; a blank name is no name at all. Integers are unaffected -- ``0`` is
    a legitimate step number, not an absent one.
    """
    mapped = map_causal_span(
        causal_span(
            "r1",
            kind="tool",
            name=blank,
            correlation_id=blank,
            tags=("keep", blank),
            metadata={"thread_id": blank, "langgraph_node": blank, "langgraph_step": 0},
        )
    )

    assert GEN_AI_TOOL_NAME not in mapped.attributes
    assert GEN_AI_CONVERSATION_ID not in mapped.attributes
    assert ZEROTH_CORRELATION_ID not in mapped.attributes
    assert LANGGRAPH_NODE not in mapped.attributes
    assert mapped.attributes[LANGGRAPH_TAGS] == ("keep",)
    assert mapped.attributes[LANGGRAPH_STEP] == 0
    # Every target source is blank, so the span name is the operation alone.
    assert mapped.name == OPERATION_EXECUTE_TOOL
    assert not [key for key, value in mapped.attributes.items() if value == ""]
    assert not [key for key, value in mapped.to_dict()["attributes"].items() if value == ""]


@pytest.mark.parametrize("blank", BLANKS)
def test_a_blank_node_name_does_not_become_the_target(blank: str) -> None:
    mapped = map_causal_span(
        causal_span("r1", kind="tool", name=None, metadata={"langgraph_node": blank})
    )

    assert GEN_AI_TOOL_NAME not in mapped.attributes
    assert LANGGRAPH_NODE not in mapped.attributes
    assert mapped.name == OPERATION_EXECUTE_TOOL


@pytest.mark.parametrize("blank", BLANKS)
def test_a_blank_run_id_is_refused_at_the_mapper(blank: str) -> None:
    with pytest.raises(ValueError, match="empty run id"):
        map_causal_span(causal_span(blank))


# -- exact-type gates: a hostile ``str`` subclass reaches no channel ------------


@pytest.mark.parametrize("slot", ["name", "correlation_id", "langgraph_node", "thread_id", "tag"])
def test_a_hostile_str_subclass_is_dropped_from_every_optional_slot(slot: str) -> None:
    """A ``str`` subclass overriding ``__format__`` / ``__str__`` must not render.

    It passes ``isinstance`` and survives ``CausalSpan.__post_init__``, so without
    an exact-type gate it would reach a ``gen_ai.*`` attribute and substitute its
    own text into the span name and the record's ``repr``.
    """
    hostile = HostileStr("planner")
    fields: dict[str, Any] = {"kind": "tool"}
    metadata: dict[str, Any] = {}
    if slot in ("name", "correlation_id"):
        fields[slot] = hostile
    elif slot == "tag":
        fields["tags"] = (hostile, "keep")
    else:
        metadata[slot] = hostile

    mapped = map_causal_span(causal_span("r1", metadata=metadata, **fields))

    assert CONTENT_SENTINEL not in mapped.name
    assert CONTENT_SENTINEL not in repr(mapped)
    assert CONTENT_SENTINEL not in json.dumps(mapped.to_dict())
    for key, value in mapped.attributes.items():
        assert CONTENT_SENTINEL not in str(value), key
        assert type(value) in (str, int, tuple), key
        if type(value) is tuple:
            assert all(type(item) is str for item in value), key
    # Nothing else names a target, so the span name is the operation alone.
    assert mapped.name == OPERATION_EXECUTE_TOOL
    assert "planner" not in json.dumps(mapped.to_dict())


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("run_id", HostileStr("r1"), "empty run id"),
        ("parent", HostileStr("r0"), "not a plain str"),
        ("kind", HostileStr("chain"), "unmappable causal span"),
        ("status", HostileStr("ok"), "unmappable causal span"),
    ],
)
def test_a_hostile_str_subclass_in_an_identity_field_is_refused(
    field: str, value: Any, match: str
) -> None:
    """Identity cannot be dropped, so an untrusted one rejects the whole record.

    A ``str`` subclass hashes equal to a contract literal, so it would pass the
    ``kind`` / ``status`` lookups and still land in ``langgraph.kind`` /
    ``zeroth.span_status``; dropping ``parent_run_id`` would silently reparent an
    orphan to a root. No ``MappedGenAiSpan`` is built in either case.
    """
    fields: dict[str, Any] = {"run_id": "r1", "kind": "chain", "status": "ok", field: value}

    with pytest.raises(ValueError, match=match) as excinfo:
        map_causal_span(causal_span(**fields))
    assert CONTENT_SENTINEL not in str(excinfo.value)


def test_a_hostile_start_reading_yields_no_duration_instead_of_an_object() -> None:
    class _HostileFloat(float):
        def __sub__(self, other: object) -> Any:
            return CONTENT_SENTINEL

    mapped = map_causal_span(causal_span("r1", start=_HostileFloat(1000.0), end=1000.5))

    assert mapped.duration_ns is None
    assert CONTENT_SENTINEL not in repr(mapped)
    assert CONTENT_SENTINEL not in json.dumps(mapped.to_dict())


# -- R7: no content channel at all --------------------------------------------


def test_importing_the_package_does_not_import_opentelemetry() -> None:
    """The emit layer is exported lazily, so the mapper stays install-safe.

    ``opentelemetry`` only ships in the optional ``otel`` extra; an eager export
    of ``emit_genai_spans`` would make ``import zeroth.integrations.langgraph``
    fail on a no-extra install. Checked in a clean subprocess so the result does
    not depend on what this session already imported.
    """
    code = (
        "import sys, zeroth.integrations.langgraph as pkg; "
        "leaked = sorted(k for k in sys.modules "
        "if k == 'opentelemetry' or k.startswith('opentelemetry.')); "
        "assert not leaked, leaked; "
        "assert 'emit_genai_spans' in pkg.__all__; "
        "assert callable(pkg.emit_genai_spans)"
    )
    completed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)

    assert completed.returncode == 0, completed.stderr


def test_unknown_package_attribute_still_raises_attribute_error() -> None:
    import zeroth.integrations.langgraph as pkg

    with pytest.raises(AttributeError, match="no attribute 'nope'"):
        pkg.nope  # noqa: B018


def test_mapper_exposes_no_content_capture_switch() -> None:
    # There is nothing to feed such a parameter: CausalSpan carries no prompts,
    # tool arguments or results, so a switch would imply a control that is fake.
    assert list(inspect.signature(map_causal_span).parameters) == ["span"]


def test_no_content_channel_carries_a_sentinel_out_of_the_record() -> None:
    mapped = map_causal_span(
        causal_span(
            "run-1",
            parent="run-0",
            kind="chat_model",
            name="gpt-router",
            status="error",
            error_type=CONTENT_SENTINEL,
            tags=("safe-tag",),
            metadata={
                "prompt": CONTENT_SENTINEL,
                "inputs": CONTENT_SENTINEL,
                "outputs": CONTENT_SENTINEL,
                "messages": CONTENT_SENTINEL,
                "langgraph_node": _SneakyStr(CONTENT_SENTINEL),
            },
        )
    )

    assert CONTENT_SENTINEL not in mapped.name
    assert CONTENT_SENTINEL not in repr(mapped)
    assert CONTENT_SENTINEL not in json.dumps(mapped.to_dict())
    for key, value in mapped.attributes.items():
        assert CONTENT_SENTINEL not in str(value), key
    # error_type is not part of the declared attribute set at all.
    assert not [key for key in mapped.attributes if "error" in key]


def test_name_and_tags_are_identity_only_and_reach_no_other_channel() -> None:
    """``name`` / ``tags`` are structural identity, not content.

    They are the only record fields a consumer sees verbatim, and R3/R4 require
    them (``gen_ai.tool.name``, the span name, ``langgraph.tags``). So the
    guarantee for them is narrower than for content: each appears **only** in its
    declared slot and nowhere else -- no duplication into ``zeroth.*``, no status
    description, no second copy under another key.
    """
    mapped = map_causal_span(causal_span("r1", kind="tool", name="IDENT_NAME", tags=("IDENT_TAG",)))

    carrying_name = {key for key, value in mapped.attributes.items() if value == "IDENT_NAME"}
    carrying_tag = {key for key, value in mapped.attributes.items() if "IDENT_TAG" in str(value)}

    assert carrying_name == {GEN_AI_TOOL_NAME}
    assert carrying_tag == {LANGGRAPH_TAGS}
    assert mapped.name == "execute_tool IDENT_NAME"


# -- timestamps ----------------------------------------------------------------


def test_duration_ns_comes_from_the_perf_counter_delta() -> None:
    mapped = map_causal_span(causal_span("r1", start=1000.0, end=1000.125))

    assert mapped.duration_ns == 125_000_000
    # No absolute timestamp is derivable from an arbitrary-origin reading, so the
    # mapped record exposes none.
    assert "start" not in mapped.to_dict()
    assert not [key for key in mapped.attributes if "time" in key or "timestamp" in key]


def test_duration_ns_is_none_while_the_span_is_still_running() -> None:
    mapped = map_causal_span(causal_span("r1", end=None, status="running"))

    assert mapped.duration_ns is None
    assert "duration_ns" not in mapped.to_dict()


# -- to_dict + goldens ---------------------------------------------------------


def test_to_dict_is_deterministic_sorted_and_omits_none() -> None:
    payload = _fully_populated().to_dict()

    assert list(payload["attributes"]) == sorted(payload["attributes"])
    assert payload["attributes"][LANGGRAPH_TAGS] == ["alpha", "beta"]
    assert json.loads(json.dumps(payload)) == payload
    root = map_causal_span(causal_span("r1")).to_dict()
    assert "parent_run_id" not in root


def test_mapped_golden_tree_matches_the_fixture() -> None:
    payload = [map_causal_span(span).to_dict() for span in golden_tree()]

    assert payload == _golden(GOLDEN_TREE, payload)


def test_golden_names_operations_parents_and_statuses_are_pinned() -> None:
    mapped = [map_causal_span(span) for span in golden_tree()]

    assert [(item.name, item.operation) for item in mapped] == [
        ("invoke_workflow governed_graph", OPERATION_INVOKE_WORKFLOW),
        ("invoke_agent planner", OPERATION_INVOKE_AGENT),
        ("chat gpt-router", OPERATION_CHAT),
        ("execute_tool search_docs", OPERATION_EXECUTE_TOOL),
        ("invoke_agent detached_node", OPERATION_INVOKE_AGENT),
    ]
    assert [(item.run_id, item.parent_run_id) for item in mapped] == [
        ("run-root", None),
        ("run-agent", "run-root"),
        ("run-chat", "run-agent"),
        ("run-tool", "run-agent"),
        ("run-orphan", "run-vanished"),
    ]
    assert [(item.span_status, item.otel_status_code) for item in mapped] == [
        ("ok", "OK"),
        ("ok", "OK"),
        ("error", "ERROR"),
        ("ok", "OK"),
        ("orphan", "UNSET"),
    ]


def test_a_missing_fixture_fails_instead_of_silently_regenerating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("REGEN_GOLDENS", raising=False)

    with pytest.raises(AssertionError, match="missing golden fixture"):
        _load("absent.json", fixtures=tmp_path)
    with pytest.raises(AssertionError, match="REGEN_GOLDENS=1"):
        _golden("absent.json", [{"any": "payload"}], fixtures=tmp_path)
    assert not list(tmp_path.iterdir())
