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


def signature_assignments() -> dict[str, str]:
    """Every ``<target>.__signature__ = ...`` in the tree, as ``module:qualname``.

    Parsed, not matched. A regex on ``^Name.__signature__ = `` misses an extra
    space, misses a qualified target like ``models.PolicyDefinition``, and -- worse
    -- reduces the target to a bare name, so a same-named class in another module
    would silently satisfy the record. The AST gives the assignment target
    exactly, and pairing it with the module makes the identity canonical.
    """
    found: dict[str, str] = {}
    for path in sorted(SOURCE.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - the tree parses
            continue
        # Module level only. A ``__signature__`` assigned inside a function body
        # belongs to an object built at call time -- ``_tool_wrappers`` publishes
        # one on each governed callable it wraps, the ordinary ``functools.wraps``
        # idiom. That cannot hide a field from a gate that pins *class*
        # signatures, so scoping to module scope is what the record governs, not
        # a convenient exclusion.
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if not isinstance(target, ast.Attribute) or target.attr != "__signature__":
                    continue
                owner = ast.unparse(target.value)
                # `module.Class` and `Class` both name the class; keep the last
                # component, but key the record by the module the assignment is in
                # so two same-named classes cannot collide.
                found[f"{_module_reference(path)}:{owner.rsplit('.', 1)[-1]}"] = (
                    path.relative_to(ROOT).as_posix()
                )
    return found


def test_no_class_hides_a_field_without_appearing_in_the_record() -> None:
    """Every ``__signature__`` assignment in the tree belongs to a recorded class.

    Without this, the record covers only the classes it already names and a new
    module could reintroduce the silence the record exists to end.
    """
    unrecorded = sorted(
        f"{reference} ({where})"
        for reference, where in signature_assignments().items()
        if reference not in HIDDEN_CONSTRUCTOR_FIELDS
    )

    assert unrecorded == [], (
        "these classes hide constructor fields from the protected-surface gate "
        f"without being recorded in HIDDEN_CONSTRUCTOR_FIELDS: {unrecorded}"
    )


def test_the_record_names_no_class_that_stopped_hiding() -> None:
    """The other direction: a recorded entry whose assignment is gone must go too."""
    assigning = set(signature_assignments())
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


def test_a_signature_assigned_inside_a_function_is_not_a_class_pin() -> None:
    """Scope is part of the rule, and the rule says so rather than filtering by name."""
    module_level = ast.parse("Klass.__signature__ = value")
    inside_function = ast.parse("def build():\n    obj.__signature__ = value\n")

    def module_scope_targets(tree: ast.Module) -> list[str]:
        return [
            target.attr
            for node in tree.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Attribute)
        ]

    assert module_scope_targets(module_level) == ["__signature__"]
    assert module_scope_targets(inside_function) == []


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
