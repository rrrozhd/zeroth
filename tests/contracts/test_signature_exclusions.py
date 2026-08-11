"""Hiding a field from the protected-surface gate is a recorded act, not a silent one.

Several classes assign ``__signature__`` to drop parameters from the signature
``inspect`` reports. The idiom is deliberate: ``tests/contracts/fixtures/
backend_surface_canonical.json`` pins constructor signatures, so a new field
would otherwise register as a public-surface change and require regenerating a
12,000-line fixture.

What was missing is the record. The exclusion sets lived in ten separate modules
with nothing naming the total, so the gate reported the surface as pinned while
the constructor had grown -- ``PolicyDefinition`` reports eight parameters and
carries fifteen fields. This module is that record: every hidden field is listed
here, the list is checked against reality in both directions, and no class may
start hiding fields without appearing in it.

The list may only shrink. Exposing a hidden field is a surface change with a
fixture-regeneration procedure nobody has written down yet, which is recorded as
a deferred observation rather than attempted here (ZER-41 / A03-14).
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import inspect
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src/zeroth"

#: Every constructor field hidden from the reported signature, by class.
#: Measured, not asserted: each entry is exactly what
#: ``declared_fields - reported_parameters`` returns today.
HIDDEN_CONSTRUCTOR_FIELDS: dict[str, tuple[str, ...]] = {
    "zeroth.contracts.langgraph_gateway.models:RunCapabilityEvidence": (
        "adapter_version",
        "inventory_fingerprint",
        "run_id",
    ),
    "zeroth.governance.audit.models:ToolCallRecord": (
        "operation_first_execution",
        "operation_key",
        "operation_reconciliation_exhausted",
        "operation_reconciliation_required",
        "operation_replay_suppressed",
        "operation_residual_duplicate_risk",
        "operation_state",
        "operation_support",
        "operation_target_ref",
    ),
    "zeroth.governance.policy.models:PolicyDefinition": (
        "allowed_assistants",
        "allowed_deployments",
        "allowed_input_classifications",
        "allowed_principals",
        "allowed_tenants",
        "max_input_bytes",
        "required_roles",
    ),
    "zeroth.governance.retention.models:ErasureResult": ("operations_deleted",),
    "zeroth.platform.config.settings:ZerothSettings": (
        "approval_notifications",
        "langgraph_gateway",
    ),
    "zeroth.runtime.orchestration.orchestrator:RuntimeOrchestrator": (
        "_token_snapshot_store",
        "operation_store",
    ),
    "zeroth.service.api.authentication:ServiceAuthConfig": (
        "custom_roles",
        "revoked_credential_ids",
    ),
    "zeroth.service.api.health:HealthResponse": ("audit_delivery", "langgraph_gateway"),
    "zeroth.service.bootstrap.container:ServiceBootstrap": (
        "audit_delivery_queue",
        "decision_repository",
        "enforcement_heartbeat_repository",
        "enforcement_stale_after_seconds",
        "inventory_registration_repository",
        "langgraph_enforcement_service",
        "langgraph_gateway_capability_reporter",
        "langgraph_gateway_compatibility",
        "langgraph_gateway_proxy",
        "langgraph_gateway_transport",
        "langgraph_gateway_websocket_handler",
        "policy_guard",
        "role_registry",
        "run_attestation_repository",
        "tool_decision_service",
        "verifier",
    ),
    "zeroth.service.langgraph_gateway.context:ReservedContextClaims": ("run_id",),
}


def _resolve(reference: str) -> Any:
    module, _, name = reference.partition(":")
    return getattr(importlib.import_module(module), name)


def declared_fields(target: Any) -> set[str]:
    """Every field the class really accepts, whatever kind of class it is.

    Pydantic models answer from ``model_fields``; dataclasses and plain classes
    answer from the *real* ``__init__``, which an assigned ``__signature__`` does
    not shadow.
    """
    fields = getattr(target, "model_fields", None)
    if fields is not None:
        return set(fields)
    if dataclasses.is_dataclass(target):
        return {field.name for field in dataclasses.fields(target)}
    return {
        name for name in inspect.signature(target.__init__).parameters if name != "self"
    }


def hidden_fields(target: Any) -> set[str]:
    """Fields the class accepts but does not report in its signature."""
    return declared_fields(target) - set(inspect.signature(target).parameters)


@pytest.mark.parametrize("reference", sorted(HIDDEN_CONSTRUCTOR_FIELDS))
def test_the_recorded_exclusions_match_what_the_class_actually_hides(reference: str) -> None:
    """Both directions, so the record cannot drift from the code either way.

    A new hidden field fails here until it is written down. A field that stops
    being hidden fails until it is removed from the list, which is what makes the
    list shrink-only rather than merely append-only.
    """
    assert hidden_fields(_resolve(reference)) == set(HIDDEN_CONSTRUCTOR_FIELDS[reference])


def _module_reference(path: Path) -> str:
    """``src/zeroth/a/b.py`` -> ``zeroth.a.b``."""
    relative = path.relative_to(SOURCE.parent).with_suffix("")
    parts = [part for part in relative.parts if part != "__init__"]
    return ".".join(parts)


#: Assignment sites verified to target something other than a class.
#:
#: An earlier attempt scoped discovery to module level on the reasoning that a
#: ``__signature__`` assigned inside a function must belong to a call-time object.
#: **That reasoning is wrong**, and the reviewer disproved it: a function-local
#: ``Klass.__signature__ = ...`` hides real constructor fields just as effectively,
#: and a module-level-only scan reports nothing. Lexical scope does not establish
#: what the target *is*.
#:
#: So the scan is recursive and the one genuinely non-class site is named here,
#: with what it was verified to be. Adding an entry is a claim about a specific
#: target, reviewable as such, rather than a rule that quietly exempts a whole
#: category.
NON_CLASS_SIGNATURE_SITES = {
    "zeroth.integrations.langgraph._tool_wrappers:governed": (
        "a per-call wrapper *function* returned by _sync_callable_wrapper / "
        "_async_callable_wrapper and published by _govern_callable -- the ordinary "
        "functools.wraps idiom, with no class constructor behind it"
    ),
}


def signature_assignments() -> dict[str, str]:
    """Every ``<target>.__signature__ = ...`` in the tree, as ``module:name``.

    Parsed, not matched, and parsed *recursively*. A regex on
    ``^Name.__signature__ = `` misses an extra space, misses a qualified target
    like ``models.PolicyDefinition``, and -- worse -- reduces the target to a bare
    name, so a same-named class in another module would silently satisfy the
    record. Restricting to module scope misses a hiding site outright.
    """
    found: dict[str, str] = {}
    for path in sorted(SOURCE.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - the tree parses
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if not isinstance(target, ast.Attribute) or target.attr != "__signature__":
                    continue
                # `module.Class` and `Class` both name the class; keep the last
                # component, but key by the module the assignment is in so two
                # same-named classes cannot collide.
                owner = ast.unparse(target.value).rsplit(".", 1)[-1]
                found[f"{_module_reference(path)}:{owner}"] = path.relative_to(ROOT).as_posix()
    return found


def class_signature_assignments() -> dict[str, str]:
    """Assignment sites that are not on the verified non-class allowlist."""
    return {
        reference: where
        for reference, where in signature_assignments().items()
        if reference not in NON_CLASS_SIGNATURE_SITES
    }


def test_no_class_hides_a_field_without_appearing_in_the_record() -> None:
    """Every ``__signature__`` assignment in the tree belongs to a recorded class.

    Without this, the record covers only the classes it already names and a new
    module could reintroduce the silence the record exists to end.
    """
    unrecorded = sorted(
        f"{reference} ({where})"
        for reference, where in class_signature_assignments().items()
        if reference not in HIDDEN_CONSTRUCTOR_FIELDS
    )

    assert unrecorded == [], (
        "these classes hide constructor fields from the protected-surface gate "
        f"without being recorded in HIDDEN_CONSTRUCTOR_FIELDS: {unrecorded}"
    )


def test_the_record_names_no_class_that_stopped_hiding() -> None:
    """The other direction: a recorded entry whose assignment is gone must go too."""
    assigning = set(class_signature_assignments())
    stale = sorted(set(HIDDEN_CONSTRUCTOR_FIELDS) - assigning)

    assert stale == [], f"recorded but no longer assigning __signature__: {stale}"


@pytest.mark.parametrize(
    "source",
    [
        "PolicyDefinition.__signature__ = value",
        "PolicyDefinition.__signature__  = value",
        "models.PolicyDefinition.__signature__ = value",
        "PolicyDefinition.__signature__ = inspect.signature(PolicyDefinition).replace()",
    ],
)
def test_the_assignment_detector_sees_every_spelling(tmp_path: Path, source: str) -> None:
    """The four shapes a line-anchored regex misses or misidentifies.

    The auditor's mutation: extra spacing and a qualified target both evaded the
    previous detector, and a bare-name key would let a same-named class in another
    module satisfy the record.
    """
    tree = ast.parse(source)
    targets = [
        ast.unparse(target.value).rsplit(".", 1)[-1]
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Attribute) and target.attr == "__signature__"
    ]

    assert targets == ["PolicyDefinition"]


def test_a_function_local_assignment_on_a_class_still_hides_fields() -> None:
    """Lexical scope does not establish what the target is.

    The disproof of an earlier, wrong rule: a ``__signature__`` assigned inside a
    function body was assumed to belong to a call-time object, so discovery
    skipped function bodies. A class pinned from inside a function hides exactly
    as much, and a module-level-only scan reports nothing at all.
    """

    @dataclasses.dataclass
    class Sample:
        visible: int = 0
        concealed: int = 0

    def pin_from_inside_a_function() -> None:
        Sample.__signature__ = inspect.signature(Sample).replace(  # type: ignore[attr-defined]
            parameters=[
                parameter
                for name, parameter in inspect.signature(Sample).parameters.items()
                if name != "concealed"
            ]
        )

    assert hidden_fields(Sample) == set()
    pin_from_inside_a_function()
    assert hidden_fields(Sample) == {"concealed"}


def test_discovery_reaches_assignments_nested_in_a_function() -> None:
    """The scan itself, over the shape the module-level rule could not see."""
    source = "def build():\n    Klass.__signature__ = value\n"
    nested = [
        ast.unparse(target.value).rsplit(".", 1)[-1]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Attribute) and target.attr == "__signature__"
    ]
    module_only = [
        target
        for node in ast.parse(source).body
        if isinstance(node, ast.Assign)
        for target in node.targets
    ]

    assert nested == ["Klass"]
    assert module_only == []


def test_every_non_class_exemption_states_what_it_was_verified_to_be() -> None:
    """An exemption without a reason is the silence this record exists to end."""
    assert NON_CLASS_SIGNATURE_SITES
    for reference, reason in NON_CLASS_SIGNATURE_SITES.items():
        assert ":" in reference, reference
        assert len(reason.strip()) > 40, reference


@pytest.mark.parametrize("reference", sorted(NON_CLASS_SIGNATURE_SITES))
def test_every_non_class_exemption_really_targets_a_non_class(reference: str) -> None:
    """The claim is checked, not taken.

    A prose reason is unfalsifiable on its own: an entry naming a real class with
    a plausible sentence beside it would escape the record entirely, which would
    make this allowlist the category exemption it replaced. So the claim is
    executed. A module-level class resolves by name; a target that does not
    resolve at module level cannot be one, which is what distinguishes a
    function-local wrapper from a pinned class.
    """
    module, _, name = reference.partition(":")
    resolved = getattr(importlib.import_module(module), name, None)

    assert not inspect.isclass(resolved), (
        f"{reference} is exempted as non-class but resolves to the class {resolved!r}; "
        "record its hidden fields in HIDDEN_CONSTRUCTOR_FIELDS instead of exempting it"
    )


def test_the_non_class_check_would_reject_a_class_named_in_the_allowlist() -> None:
    """The detector, fed the escape it exists to refuse."""
    honest = getattr(
        importlib.import_module("zeroth.integrations.langgraph._tool_wrappers"),
        "governed",
        None,
    )
    a_real_class = importlib.import_module(
        "zeroth.governance.policy.models"
    ).PolicyDefinition

    assert not inspect.isclass(honest)
    assert inspect.isclass(a_real_class)


def test_the_non_class_allowlist_names_only_sites_that_exist() -> None:
    """A retired exemption would let a real hiding site inherit its pass."""
    stale = sorted(set(NON_CLASS_SIGNATURE_SITES) - set(signature_assignments()))

    assert stale == [], f"exempted but no longer present: {stale}"


def test_the_record_names_no_class_that_hides_nothing() -> None:
    """A stale entry would let a real exclusion hide behind a retired one."""
    empty = sorted(
        reference for reference, fields in HIDDEN_CONSTRUCTOR_FIELDS.items() if not fields
    )

    assert empty == []


def test_the_helper_sees_a_hidden_field_it_was_not_told_about() -> None:
    """The detector, fed a class that hides a field the record does not name.

    Without this the assertions above could pass because ``hidden_fields`` always
    returns the empty set, which is exactly the failure mode this file exists to
    prevent elsewhere.
    """

    @dataclasses.dataclass
    class Sample:
        visible: int = 0
        concealed: int = 0

    assert hidden_fields(Sample) == set()

    Sample.__signature__ = inspect.signature(Sample).replace(  # type: ignore[attr-defined]
        parameters=[
            parameter
            for name, parameter in inspect.signature(Sample).parameters.items()
            if name != "concealed"
        ]
    )

    assert hidden_fields(Sample) == {"concealed"}


def test_the_measured_totals_are_what_the_audit_reported() -> None:
    """A03-14's two named classes, pinned by count so the record cannot be padded."""
    policy = _resolve("zeroth.governance.policy.models:PolicyDefinition")
    tool_call = _resolve("zeroth.governance.audit.models:ToolCallRecord")

    assert len(declared_fields(policy)) == 15
    assert len(inspect.signature(policy).parameters) == 8
    assert len(hidden_fields(tool_call)) == 9
