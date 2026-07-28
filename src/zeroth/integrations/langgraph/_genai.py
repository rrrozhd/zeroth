"""Versioned mapping of neutral causal spans onto OpenTelemetry GenAI spans (ZER-4).

This module is the **single mapping boundary** between the collection contract
(:class:`~zeroth.integrations.langgraph._spans.CausalSpan`, ZER-3) and the
OpenTelemetry GenAI semantic conventions. It is deliberately **pure**: it
imports neither ``opentelemetry`` nor ``langgraph``, so importing the package
costs nothing and works without the optional ``otel`` extra. Turning mapped
records into real spans is :mod:`zeroth.integrations.langgraph._genai_emit`.

Three disjoint attribute namespaces are emitted and nothing else:

* ``gen_ai.*`` -- standard semconv identifiers only, **vendored** below rather
  than imported from ``opentelemetry.semconv``, whose GenAI module is private,
  incubating and only transitively installed; importing it would break a no-extra
  install. ``genai/test_semconv_drift.py`` compares the two when both exist.
* ``langgraph.*`` -- framework ancestry and structural context.
* ``zeroth.*`` -- governance metadata, including the **unverified** gateway
  correlation id, which must never appear under ``gen_ai.*``.

Being the boundary, this module gates **every** value it can emit (see
:func:`_plain_scalar`): the type check is exact -- ``type(x) is str`` /
``type(x) is int``, never ``isinstance`` -- and a blank string counts as
*absent*, so no attribute is ever emitted as ``""``. Nothing upstream enforces
either rule: ``bool`` is an ``int`` subclass, a ``str`` subclass can override
``__format__`` / ``__str__`` / ``__repr__`` to substitute text once formatted
into a span name or a ``repr``, and ``CausalSpan.__post_init__`` filters metadata
with ``isinstance`` while leaving its other fields unvalidated.

Privacy is structural, not configurable: ``CausalSpan`` carries no prompts, tool
arguments, results or free-form metadata, so there is no content channel to gate
and deliberately **no** ``capture_content`` parameter -- a switch over an empty
channel would only imply a guarantee this layer cannot make. Model, provider and
token ids are absent too: ``_handler.on_llm_end`` discards the ``LLMResult``, so
semconv's "when available" honestly resolves to *absent* here. Bumping
:data:`GENAI_CONVENTION_VERSION` must change only this mapper's output and its
golden fixtures; the collection contract is never touched.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, Literal, cast

from zeroth.integrations.langgraph._spans import CausalSpan, SpanKind, SpanStatus

GENAI_CONVENTION_VERSION: Final[str] = "1.0.0"
"""Stamped on every span as ``zeroth.convention_version``: a consumer pins the
emitted attribute shape by this value alone; bumping it never touches collection.
"""

GEN_AI_NAMESPACE: Final[str] = "gen_ai."
LANGGRAPH_NAMESPACE: Final[str] = "langgraph."
ZEROTH_NAMESPACE: Final[str] = "zeroth."

ATTRIBUTE_NAMESPACES: Final[tuple[str, ...]] = (
    GEN_AI_NAMESPACE,
    LANGGRAPH_NAMESPACE,
    ZEROTH_NAMESPACE,
)
"""The only prefixes a mapped span may use. Kept disjoint by construction."""

# -- vendored ``gen_ai.*`` semantic-convention attribute keys ------------------

GEN_AI_OPERATION_NAME: Final[str] = "gen_ai.operation.name"
GEN_AI_TOOL_NAME: Final[str] = "gen_ai.tool.name"
GEN_AI_AGENT_NAME: Final[str] = "gen_ai.agent.name"
GEN_AI_WORKFLOW_NAME: Final[str] = "gen_ai.workflow.name"
GEN_AI_CONVERSATION_ID: Final[str] = "gen_ai.conversation.id"

# -- vendored ``gen_ai.operation.name`` values ---------------------------------

OPERATION_CHAT: Final[str] = "chat"
OPERATION_EXECUTE_TOOL: Final[str] = "execute_tool"
OPERATION_INVOKE_AGENT: Final[str] = "invoke_agent"
OPERATION_INVOKE_WORKFLOW: Final[str] = "invoke_workflow"
OPERATION_RETRIEVAL: Final[str] = "retrieval"
OPERATION_EMBEDDINGS: Final[str] = "embeddings"

# -- ``langgraph.*`` keys ------------------------------------------------------

LANGGRAPH_RUN_ID: Final[str] = "langgraph.run_id"
LANGGRAPH_PARENT_RUN_ID: Final[str] = "langgraph.parent_run_id"
LANGGRAPH_KIND: Final[str] = "langgraph.kind"
LANGGRAPH_NODE: Final[str] = "langgraph.node"
LANGGRAPH_STEP: Final[str] = "langgraph.step"
LANGGRAPH_TAGS: Final[str] = "langgraph.tags"

# -- ``zeroth.*`` keys ---------------------------------------------------------

ZEROTH_CORRELATION_ID: Final[str] = "zeroth.correlation_id"
ZEROTH_CONVENTION_VERSION: Final[str] = "zeroth.convention_version"
ZEROTH_SPAN_STATUS: Final[str] = "zeroth.span_status"

_METADATA_ALLOWLIST: Final[tuple[str, ...]] = ("langgraph_node", "langgraph_step", "thread_id")
"""Metadata keys this mapper reads. Mirrors collection's whitelist; nothing else maps."""

_OPERATION_BY_KIND: Final[Mapping[tuple[SpanKind, bool], str]] = MappingProxyType(
    {
        ("tool", True): OPERATION_EXECUTE_TOOL,
        ("tool", False): OPERATION_EXECUTE_TOOL,
        ("llm", True): OPERATION_CHAT,
        ("llm", False): OPERATION_CHAT,
        ("chat_model", True): OPERATION_CHAT,
        ("chat_model", False): OPERATION_CHAT,
        ("chain", True): OPERATION_INVOKE_WORKFLOW,
        ("chain", False): OPERATION_INVOKE_AGENT,
    }
)
"""``(kind, is_root)`` -> ``gen_ai.operation.name``, exhaustive over :data:`SpanKind`.

The boolean is ``parent_run_id is None``. It matters only for ``chain``, the one
kind LangGraph uses for both a whole graph and a node inside it: a root chain is
the workflow (``invoke_workflow``), a nested chain an agent step
(``invoke_agent``). Root-ness is the *only* disambiguation a ``CausalSpan``
allows -- it carries no "graph or node" flag, and inventing one would change
collection.
"""

UNREACHABLE_OPERATION_NAMES: Final[frozenset[str]] = frozenset(
    {OPERATION_RETRIEVAL, OPERATION_EMBEDDINGS}
)
"""Semconv operations this mapper defines but can never produce.

Collection registers no retriever or embeddings callbacks, so no record can carry
those kinds. Vendored for completeness and pinned by tests; reaching them means a
collection-contract change, out of scope.
"""

GENAI_OPERATION_NAMES: Final[frozenset[str]] = (
    frozenset(_OPERATION_BY_KIND.values()) | UNREACHABLE_OPERATION_NAMES
)
"""Every ``gen_ai.operation.name`` value this module vendors."""

_TARGET_ATTRIBUTE_BY_OPERATION: Final[Mapping[str, str]] = MappingProxyType(
    {
        OPERATION_EXECUTE_TOOL: GEN_AI_TOOL_NAME,
        OPERATION_INVOKE_AGENT: GEN_AI_AGENT_NAME,
        OPERATION_INVOKE_WORKFLOW: GEN_AI_WORKFLOW_NAME,
    }
)
"""Operation -> the ``gen_ai.*`` identifier the resolved target names.

``chat`` has no entry: semconv names a chat span's subject with
``gen_ai.request.model`` / ``gen_ai.provider.name``, neither of which a record
carries, and the LangChain runnable name is not a model id.
"""

OtelStatusCode = Literal["UNSET", "OK", "ERROR"]
"""The OTel status this mapping picks, as a plain string (see :data:`_OTEL_STATUS`)."""

_OTEL_STATUS: Final[Mapping[SpanStatus, OtelStatusCode]] = MappingProxyType(
    {"running": "UNSET", "ok": "OK", "error": "ERROR", "orphan": "UNSET"}
)
"""Neutral :data:`SpanStatus` -> OTel status code.

``orphan`` is a property of the *tree* (a dangling ``parent_run_id``), not a run
failure, so it stays ``UNSET``; the neutral value survives verbatim in
``zeroth.span_status``, which is where a consumer reads it.
"""

AttributeValue = str | int | tuple[str, ...]
"""The OTel attribute value types this mapper can produce."""


@dataclass(frozen=True)
class PerfCounterAnchor:
    """One instant sampled on both clocks, tying ``perf_counter`` to epoch time.

    ``CausalSpan.start`` / ``end`` are ``time.perf_counter()`` readings: monotonic,
    with an **arbitrary origin**, so they cannot become wall-clock timestamps on
    their own. A caller that read both clocks at the same instant passes the pair
    here; without it the SDK stamps emission time and only ``duration_ns`` means
    anything.

    Attributes:
        perf_counter: A ``time.perf_counter()`` reading.
        epoch_ns: ``time.time_ns()`` sampled at the same instant.
    """

    perf_counter: float
    epoch_ns: int


@dataclass(frozen=True)
class MappedGenAiSpan:
    """One :class:`CausalSpan` expressed in the GenAI convention, without emitting.

    Carries ``run_id`` / ``parent_run_id`` unchanged so the emit layer can rebuild
    the real span tree, and no absolute timestamps: only ``duration_ns``.

    Attributes:
        run_id: The causal span's run id, verbatim.
        parent_run_id: The parent run id, verbatim (``None`` for a tree root); a
            dangling reference is preserved, never reparented.
        name: OTel span name -- ``"{operation} {target}"``, or the operation alone
            when the record names no target.
        operation: The ``gen_ai.operation.name`` value.
        span_status: The neutral :data:`SpanStatus`, including ``orphan``.
        otel_status_code: The OTel status this mapping picks for ``span_status``.
        duration_ns: ``end - start`` in nanoseconds, or ``None`` when unknown.
        attributes: The full attribute set, immutable, confined to
            :data:`ATTRIBUTE_NAMESPACES`.
    """

    run_id: str
    parent_run_id: str | None
    name: str
    operation: str
    span_status: SpanStatus
    otel_status_code: OtelStatusCode
    duration_ns: int | None
    attributes: Mapping[str, AttributeValue]

    def to_dict(self) -> dict[str, Any]:
        """Return a deterministic, JSON-safe view for golden comparison.

        Keys are sorted, tuples become lists (so a JSON round trip compares equal)
        and ``None`` values are omitted rather than serialised.
        """
        payload: dict[str, Any] = {
            "run_id": self.run_id,
            "parent_run_id": self.parent_run_id,
            "name": self.name,
            "operation": self.operation,
            "span_status": self.span_status,
            "otel_status_code": self.otel_status_code,
            "duration_ns": self.duration_ns,
            "attributes": {
                key: list(value) if isinstance(value, tuple) else value
                for key, value in sorted(self.attributes.items())
            },
        }
        return {key: value for key, value in payload.items() if value is not None}


def _plain_scalar(value: Any) -> str | int | None:
    """Apply the boundary gate stated in the module docstring (``0`` is not blank)."""
    if type(value) is str:
        return value if value.strip() else None
    return value if type(value) is int else None


def _plain_str(value: Any) -> str | None:
    """Return ``value`` when :func:`_plain_scalar` admits it *as a string*."""
    admitted = _plain_scalar(value)
    return admitted if type(admitted) is str else None


def _duration_ns(span: CausalSpan) -> int | None:
    """Return ``end - start`` in nanoseconds, or ``None`` when not derivable.

    A ``perf_counter`` delta -- arbitrary origin, never a wall clock. Both
    readings must be exactly ``float`` / ``int``: a subclass could override
    ``__sub__`` and choose what lands in ``duration_ns`` and so in the ``repr``.
    """
    if type(span.start) not in (float, int) or type(span.end) not in (float, int):
        return None
    return round((span.end - span.start) * 1_000_000_000)


def _allowed_metadata(metadata: Mapping[str, Any]) -> dict[str, str | int]:
    """Return the allowlisted metadata entries whose value passes the gate.

    Rogue keys, floats, containers, blanks and subclasses are dropped silently.

    Entries are *iterated* rather than looked up by key. A ``dict``/``Mapping``
    lookup runs the stored key's ``__hash__`` / ``__eq__``, so a ``str`` subclass
    key could execute its own code inside the mapper -- spoofing equality with an
    allowlisted name, or raising an exception carrying arbitrary text. Gating
    ``type(key) is str`` *before* any comparison means such a key is skipped
    without its code ever running.
    """
    allowed: dict[str, str | int] = {}
    for key, raw in metadata.items():
        if type(key) is not str or key not in _METADATA_ALLOWLIST:
            continue
        value = _plain_scalar(raw)
        if value is not None:
            allowed[key] = value
    return allowed


def _resolve_target(span: CausalSpan, metadata: Mapping[str, str | int]) -> str | None:
    """Return the span's target name: the record's ``name``, else its node name.

    ``None`` -- the record names no target, so the span name is the operation
    alone -- also covers a ``name`` that is blank or a ``str`` subclass.
    """
    return _plain_str(span.name) or _plain_str(metadata.get("langgraph_node"))


def _gen_ai_attributes(
    operation: str, target: str | None, metadata: Mapping[str, str | int]
) -> dict[str, AttributeValue]:
    """Return the standard ``gen_ai.*`` identifiers, omitting absent ones entirely.

    Never null or empty: an identifier the record cannot supply is left out, which
    is how semconv's "when available" is satisfied honestly.
    """
    attributes: dict[str, AttributeValue] = {GEN_AI_OPERATION_NAME: operation}
    target_key = _TARGET_ATTRIBUTE_BY_OPERATION.get(operation)
    if target is not None and target_key is not None:
        attributes[target_key] = target
    thread_id = metadata.get("thread_id")
    if thread_id is not None:
        attributes[GEN_AI_CONVERSATION_ID] = thread_id
    return attributes


def _langgraph_attributes(
    span: CausalSpan, run_id: str, kind: str, metadata: Mapping[str, str | int]
) -> dict[str, AttributeValue]:
    """Return the ``langgraph.*`` ancestry and structural context attributes.

    ``run_id`` / ``kind`` / ``metadata`` arrive gated and the caller has verified
    ``parent_run_id``, so an orphan's dangling reference passes through verbatim.
    """
    attributes: dict[str, AttributeValue] = {LANGGRAPH_RUN_ID: run_id, LANGGRAPH_KIND: kind}
    if span.parent_run_id is not None:
        attributes[LANGGRAPH_PARENT_RUN_ID] = span.parent_run_id
    node = metadata.get("langgraph_node")
    if node is not None:
        attributes[LANGGRAPH_NODE] = node
    step = metadata.get("langgraph_step")
    if step is not None:
        attributes[LANGGRAPH_STEP] = step
    # OTel rejects a heterogeneous sequence and callbacks may hand the handler
    # non-string tags, so every entry goes through the same gate. The container
    # itself is gated too: a tuple subclass may override ``__iter__`` and yield
    # entries it never stored, so anything but an exact tuple is omitted.
    if type(span.tags) is tuple:
        tags = tuple(tag for tag in map(_plain_str, span.tags) if tag is not None)
        if tags:
            attributes[LANGGRAPH_TAGS] = tags
    return attributes


def _zeroth_attributes(status: str, correlation_id: str | None) -> dict[str, AttributeValue]:
    """Return the ``zeroth.*`` governance attributes; both inputs arrive gated.

    ``zeroth.correlation_id`` is the UNVERIFIED gateway correlation, extracted
    from a reserved-context token without signature checking. It is governance
    metadata and must never be published as a ``gen_ai.*`` identifier, where a
    consumer would read it as a trusted conversation key.
    """
    attributes: dict[str, AttributeValue] = {
        ZEROTH_CONVENTION_VERSION: GENAI_CONVENTION_VERSION,
        ZEROTH_SPAN_STATUS: status,
    }
    if correlation_id is not None:
        attributes[ZEROTH_CORRELATION_ID] = correlation_id
    return attributes


def map_causal_span(span: CausalSpan) -> MappedGenAiSpan:
    """Map one causal span onto the GenAI convention. Pure; emits nothing.

    Every value crosses the boundary gate described in the module docstring. A
    failing optional one is dropped, but identity cannot be: dropping
    ``parent_run_id`` would silently reparent an orphan to a root, and a ``str``
    subclass of ``kind`` / ``status`` hashes equal to a contract literal, passing
    the lookups below while still reaching ``langgraph.kind``,
    ``zeroth.span_status`` and this record's ``repr``. So the record is refused.

    Args:
        span: A record read from ``ZerothGovernanceCallbackHandler``'s batch
            accessors, where the read-time orphan determination has run.

    Returns:
        The mapped span: OTel span name, operation, statuses, ``duration_ns``
        and the full attribute set.

    Raises:
        ValueError: If ``kind`` or ``status`` is outside the collection contract,
            or if ``run_id`` / ``parent_run_id`` / ``kind`` / ``status`` is not a
            plain non-blank ``str``. Nothing is emitted in either case.
    """
    metadata = _allowed_metadata(span.metadata)
    run_id = _plain_str(span.run_id)
    kind = _plain_str(span.kind)
    status = _plain_str(span.status)
    operation = _OPERATION_BY_KIND.get((cast(SpanKind, kind), span.parent_run_id is None))
    otel_status = _OTEL_STATUS.get(cast(SpanStatus, status))
    if run_id is None:
        raise ValueError("cannot map a causal span with an empty run id, or one that is not a str")
    if span.parent_run_id is not None and _plain_str(span.parent_run_id) is None:
        raise ValueError("causal span parent_run_id is not a plain str; refusing to reparent it")
    if operation is None or otel_status is None:
        raise ValueError(f"unmappable causal span: kind={kind!r} status={status!r}")
    target = _resolve_target(span, metadata)
    attributes: dict[str, AttributeValue] = {
        **_gen_ai_attributes(operation, target, metadata),
        **_langgraph_attributes(span, run_id, kind, metadata),
        **_zeroth_attributes(status, _plain_str(span.correlation_id)),
    }
    return MappedGenAiSpan(
        run_id=run_id,
        parent_run_id=span.parent_run_id,
        name=f"{operation} {target}" if target else operation,
        operation=operation,
        span_status=cast(SpanStatus, status),
        otel_status_code=otel_status,
        duration_ns=_duration_ns(span),
        attributes=MappingProxyType(attributes),
    )


__all__ = ["GENAI_CONVENTION_VERSION", "MappedGenAiSpan", "PerfCounterAnchor", "map_causal_span"]
