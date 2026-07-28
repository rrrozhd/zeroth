"""What normalization guarantees before any policy is allowed to look at a call.

Three properties are load-bearing. First, the descriptor is *complete* before a
decision exists: every field of the ``ToolAction`` is populated by
``normalize_tool_action``, and this module imports nothing from the decision
seam, so the ordering is structural rather than a convention someone could
reorder. Second, the principal is injected -- a forged correlation id, the one
value the integration carries in ambient state and the one it explicitly does
not trust, cannot become a principal. Third, every gate is an exact-type gate:
the builtin subtypes in ``_hostile`` are admitted by ``isinstance`` and refused
here.

No ``langgraph`` or ``langchain`` import, hence no ``langgraph_conformance``
marker: ``addopts`` deselects that marker, so a marked test would never run.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect

import pytest

from tests.integrations.langgraph.genai._causal import BLANKS
from tests.integrations.langgraph.tools._hostile import (
    CONTENT_SENTINEL,
    HostileDict,
    HostileKey,
    HostileList,
    HostileStr,
    HostileTuple,
)
from zeroth.integrations.langgraph import _correlation, _tool_normalize
from zeroth.integrations.langgraph._tool_errors import (
    GovernanceContextError,
    ToolGovernanceError,
    UnstableToolIdentityError,
)
from zeroth.integrations.langgraph._tool_normalize import (
    argument_fingerprint,
    canonical_arguments,
    classify_side_effect,
    normalize_contract_ref,
    normalize_tool_action,
    normalize_tool_identity,
    require_governance_context,
)
from zeroth.integrations.langgraph._tool_types import (
    SideEffectClass,
    ToolAction,
    ToolGovernanceContext,
)

CONTEXT = ToolGovernanceContext(
    tenant_id="tenant-a",
    principal_id="principal-1",
    run_id="run-1",
    thread_id="thread-1",
    correlation_id="correlation-1",
)

FORGED_CORRELATION = "ATTACKER_CONTROLLED_PRINCIPAL_7f21"
"""Published on the correlation carrier to prove it never becomes an identity."""


def action(**overrides) -> ToolAction:
    """Normalize a representative tool call, with the given fields overridden."""
    kwargs = {
        "name": "search",
        "arguments": {"query": "weather"},
        "context": CONTEXT,
        "side_effect": SideEffectClass.READ_ONLY,
    }
    kwargs.update(overrides)
    return normalize_tool_action(**kwargs)


def rendered(normalized: ToolAction) -> str:
    """Render every field of an action the way a log line or an f-string would.

    Formatting, not ``json.dumps``: a ``HostileStr`` serializes as its real
    characters through the JSON encoder and only reveals itself through
    ``__format__`` / ``__str__``, which is exactly the hook a downstream logger
    or audit renderer would hit.
    """
    parts = [
        f"{normalized.identity.name}",
        f"{normalized.identity.fingerprint}",
        f"{normalized.contract_ref}",
        f"{normalized.principal_id}",
        f"{normalized.side_effect}",
        *[f"{key}={value}" for key, value in normalized.arguments.items()],
    ]
    return "|".join(parts)


class NotAContext:
    """A duck-typed impostor carrying every field a governance context has."""

    tenant_id = "tenant-a"
    principal_id = "attacker"
    run_id = "run-1"
    thread_id = None
    correlation_id = None


@dataclasses.dataclass(frozen=True, slots=True)
class ContextSubclass(ToolGovernanceContext):
    """A subclass ``isinstance`` accepts and the exact-type gate does not."""


# --- the descriptor exists, complete, before anything decides anything ---------


def test_normalization_populates_every_field_of_the_decision_input() -> None:
    # R3: what a policy is handed is fully built first -- not a name plus a bag
    # of whatever the caller passed, assembled as the decision is made.
    normalized = action(identity_material={"schema": {"query": "string"}}, contract_ref="c:search")

    assert normalized.identity.name == "search"
    assert len(normalized.identity.fingerprint) == 64
    assert dict(normalized.arguments) == {"query": "weather"}
    assert normalized.contract_ref == "c:search"
    assert normalized.principal_id == "principal-1"
    assert normalized.side_effect is SideEffectClass.READ_ONLY


def imported_modules(module: object) -> set[str]:
    """Collect every module name *module* imports, read off its parsed source.

    Parsed rather than grepped: the module docstrings here discuss the modules
    they deliberately do *not* import, and a substring search cannot tell an
    explanation from a dependency.
    """
    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_the_normalizer_does_not_import_the_decision_seam() -> None:
    # Structural, because the behavioural version of this ("no client was
    # called") survives only as long as nobody adds a call. An import edge from
    # normalization to decisions is what "normalize first" would have to break.
    imported = imported_modules(_tool_normalize)

    assert not [name for name in imported if "_tool_decisions" in name]


# --- the principal is injected, never inherited from ambient state ------------


def test_the_normalizer_reads_nothing_from_the_correlation_carrier() -> None:
    # _correlation.py:20-27 says its value is base64url-decoded with no
    # signature check and that enforced mode must never inherit trust from it.
    imported = imported_modules(_tool_normalize)

    assert not [name for name in imported if "_correlation" in name]
    assert "current_correlation" not in vars(_tool_normalize)


def test_a_forged_correlation_never_becomes_a_principal() -> None:
    # The behavioural half: even with the carrier published, the principal is
    # the injected one and the forged value appears nowhere in the descriptor.
    token = _correlation.set_correlation(FORGED_CORRELATION)
    try:
        normalized = action()
    finally:
        _correlation.reset_correlation(token)

    assert normalized.principal_id == "principal-1"
    assert FORGED_CORRELATION not in rendered(normalized)


def test_a_call_with_no_governance_context_is_refused_not_attributed() -> None:
    with pytest.raises(GovernanceContextError):
        action(context=None)


@pytest.mark.parametrize(
    "context",
    [
        NotAContext(),
        {"tenant_id": "t", "principal_id": "p", "run_id": "r"},
        ContextSubclass(tenant_id="t", principal_id="attacker", run_id="r"),
    ],
    ids=["duck_typed", "mapping", "subclass"],
)
def test_only_an_exact_governance_context_can_attribute_a_call(context: object) -> None:
    # The subclass case is the one isinstance would wave through.
    with pytest.raises(GovernanceContextError):
        require_governance_context(context)


@pytest.mark.parametrize("field", ["tenant_id", "principal_id", "run_id"])
@pytest.mark.parametrize("blank", BLANKS, ids=["empty", "space", "whitespace"])
def test_a_blank_required_context_field_is_absent_not_empty(field: str, blank: str) -> None:
    with pytest.raises(GovernanceContextError):
        require_governance_context(dataclasses.replace(CONTEXT, **{field: blank}))


@pytest.mark.parametrize("field", ["tenant_id", "principal_id", "run_id"])
def test_a_hostile_string_cannot_ride_into_a_context_field(field: str) -> None:
    with pytest.raises(GovernanceContextError):
        require_governance_context(dataclasses.replace(CONTEXT, **{field: HostileStr("x")}))


def test_the_context_is_rebuilt_from_gated_fields_rather_than_kept() -> None:
    # Copy-then-validate: the returned object is new, and its optional fields
    # are plain strings or absent -- never the caller's object.
    normalized = require_governance_context(
        dataclasses.replace(CONTEXT, thread_id="  thread-9  ", correlation_id=HostileStr("x"))
    )

    assert normalized is not CONTEXT
    assert normalized.thread_id == "thread-9"
    assert normalized.correlation_id is None
    assert type(normalized.thread_id) is str


# --- identity -----------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [HostileStr("search"), *BLANKS, None, 7, b"search", "n" * 257],
    ids=["hostile_str", "empty", "space", "whitespace", "none", "int", "bytes", "over_long"],
)
def test_a_tool_whose_name_is_not_a_plain_bounded_string_has_no_identity(name: object) -> None:
    with pytest.raises(UnstableToolIdentityError):
        normalize_tool_identity(name)


@pytest.mark.parametrize(
    "material",
    [
        {"schema": HostileStr("string")},
        {HostileKey("schema"): "string"},
        {"schema": HostileTuple()},
        HostileDict(schema="string"),
        {"schema": object()},
    ],
    ids=["hostile_value", "hostile_key", "hostile_tuple", "hostile_mapping", "opaque"],
)
def test_identity_material_that_cannot_be_projected_leaves_the_tool_unpinned(
    material: object,
) -> None:
    # Reported as an unstable identity rather than an argument failure: the
    # fingerprint is what a policy is written against.
    with pytest.raises(UnstableToolIdentityError):
        normalize_tool_identity("search", material)


def test_the_same_tool_fingerprints_the_same_and_a_different_one_does_not() -> None:
    material = {"schema": {"query": "string"}}

    assert normalize_tool_identity("search", material) == normalize_tool_identity(
        "  search  ", dict(material)
    )
    assert (
        normalize_tool_identity("search", material).fingerprint
        != normalize_tool_identity("search", {"schema": {"query": "int"}}).fingerprint
    )
    assert (
        normalize_tool_identity("search", material).fingerprint
        != normalize_tool_identity("delete", material).fingerprint
    )


# --- canonical arguments ------------------------------------------------------


def test_the_json_shaped_argument_types_survive_the_projection() -> None:
    arguments = {"s": "x", "i": 1, "f": 1.5, "b": True, "n": None, "l": [1, {"k": "v"}]}

    assert dict(canonical_arguments(arguments)) == arguments


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": HostileStr("weather")},
        {HostileKey("query"): "weather"},
        {"query": HostileTuple()},
        {"query": HostileList()},
        {"query": HostileDict(k="v")},
        {"query": {"nested": HostileStr("weather")}},
        {"query": [HostileStr("weather")]},
        {1: "weather"},
        {"query": object()},
        {"query": {1, 2}},
    ],
    ids=[
        "hostile_value",
        "hostile_key",
        "hostile_tuple",
        "hostile_list",
        "hostile_mapping_value",
        "nested_hostile_value",
        "hostile_in_sequence",
        "non_string_key",
        "opaque_object",
        "set",
    ],
)
def test_an_argument_that_is_not_exactly_a_json_type_is_refused(arguments: dict) -> None:
    # Refused, never elided and never replaced by a placeholder: a policy that
    # denies on an argument's value must not be shown a different value.
    with pytest.raises(ToolGovernanceError):
        canonical_arguments(arguments)


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf")],
    ids=["nan", "inf", "negative_inf"],
)
@pytest.mark.parametrize(
    "shape",
    [
        lambda value: {"threshold": value},
        lambda value: {"limits": [value]},
        lambda value: {"limits": {"upper": value}},
    ],
    ids=["scalar", "in_sequence", "nested"],
)
def test_a_non_finite_float_is_not_canonical_json_and_is_refused(shape, value: float) -> None:
    # ``json.dumps`` spells these ``NaN`` / ``Infinity``, which no other parser
    # reads back, so the fingerprint and the audit record stop round-tripping.
    # ``NaN`` also compares unequal to itself, so a policy cannot decide an
    # argument carrying one by comparison and a replay cannot match it.
    with pytest.raises(ToolGovernanceError):
        canonical_arguments(shape(value))

    with pytest.raises(ToolGovernanceError):
        argument_fingerprint(shape(value))


def test_a_finite_float_still_survives_the_projection() -> None:
    # The positive control: the gate rejects the three non-finite values and
    # nothing else, including the ones that sit near a float's limits.
    arguments = {"small": 1.5e-308, "large": 1.7e308, "zero": -0.0}

    assert dict(canonical_arguments(arguments)) == arguments


@pytest.mark.parametrize(
    "source",
    [HostileDict(query="weather"), None, [("query", "weather")], "query=weather"],
    ids=["hostile_dict", "none", "pairs", "string"],
)
def test_only_an_exact_mapping_can_carry_arguments(source: object) -> None:
    with pytest.raises(ToolGovernanceError):
        canonical_arguments(source)


def test_a_hostile_key_is_never_stored_so_its_hooks_never_run_later() -> None:
    # HostileKey counts its own __hash__/__eq__ calls. Building the literal
    # below hashes it once; after that the normalizer must never touch it again.
    HostileKey.hashed = 0
    HostileKey.compared = 0
    hostile = {HostileKey("query"): "weather"}
    baseline = HostileKey.hashed

    with pytest.raises(ToolGovernanceError):
        canonical_arguments(hostile)

    assert HostileKey.hashed == baseline


def test_arguments_nested_past_the_projection_depth_are_refused() -> None:
    deep: dict = {"leaf": 1}
    for _ in range(32):
        deep = {"next": deep}

    with pytest.raises(ToolGovernanceError):
        canonical_arguments(deep)


def test_an_oversized_argument_structure_is_refused() -> None:
    with pytest.raises(ToolGovernanceError):
        canonical_arguments({"wide": list(range(8192))})


def test_a_later_mutation_of_a_nested_argument_cannot_reach_the_action() -> None:
    # The snapshot is deep: the caller keeps a reference to the inner dict, and
    # the decision was made about the copy.
    inner = {"depth": "original"}
    normalized = action(arguments={"outer": inner})

    inner["depth"] = CONTENT_SENTINEL

    assert normalized.arguments["outer"] == {"depth": "original"}


# --- the argument fingerprint -------------------------------------------------


def test_equal_arguments_fingerprint_alike_whatever_order_they_were_built_in() -> None:
    assert argument_fingerprint({"a": 1, "b": {"c": [1, 2]}}) == argument_fingerprint(
        {"b": {"c": [1, 2]}, "a": 1}
    )


@pytest.mark.parametrize(
    "other",
    [{"a": 2}, {"a": "1"}, {"a": 1, "b": None}, {"b": 1}, {}],
    ids=["value", "type", "extra_key", "key", "empty"],
)
def test_different_arguments_fingerprint_differently(other: dict) -> None:
    assert argument_fingerprint({"a": 1}) != argument_fingerprint(other)


def test_the_fingerprint_can_be_recomputed_from_an_action_that_was_already_built() -> None:
    normalized = action(arguments={"query": "weather"})

    assert argument_fingerprint(normalized.arguments) == argument_fingerprint({"query": "weather"})


# --- contract binding and side-effect classification --------------------------


@pytest.mark.parametrize(
    "value",
    [HostileStr("c:search"), *BLANKS, None, 7],
    ids=["hostile_str", "empty", "space", "whitespace", "none", "int"],
)
def test_an_unusable_contract_reference_is_absent_rather_than_carried(value: object) -> None:
    # Absent is a real answer for a contract -- a tool may declare none -- so
    # this normalizes to None instead of raising. What it must never do is keep
    # the hostile object.
    assert normalize_contract_ref(value) is None


def test_a_hostile_contract_reference_leaves_no_trace_in_the_action() -> None:
    normalized = action(contract_ref=HostileStr("c:search"))

    assert normalized.contract_ref is None
    assert CONTENT_SENTINEL not in rendered(normalized)


@pytest.mark.parametrize(
    "value",
    ["read_only", "side_effecting", None, True, 1, HostileStr("read_only")],
    ids=["bare_string", "bare_side_effecting", "none", "true", "int", "hostile_str"],
)
def test_a_classification_nobody_made_is_unknown_not_the_value_it_resembles(value: object) -> None:
    # SideEffectClass is a StrEnum, so "read_only" == SideEffectClass.READ_ONLY.
    # An equality gate here would let an unclassified tool present as read-only.
    assert classify_side_effect(value) is SideEffectClass.UNKNOWN


@pytest.mark.parametrize("member", list(SideEffectClass))
def test_an_actual_classification_survives(member: SideEffectClass) -> None:
    assert classify_side_effect(member) is member


def test_an_unclassified_call_normalizes_to_unknown_rather_than_failing() -> None:
    # Normalization does not decide; UNKNOWN is a descriptor value, and it is
    # the decision stage that refuses to act on it.
    assert action(side_effect=None).side_effect is SideEffectClass.UNKNOWN
