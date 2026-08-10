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

import dataclasses
import importlib
import inspect
import re
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


def test_no_class_hides_a_field_without_appearing_in_the_record() -> None:
    """Every ``__signature__`` assignment in the tree belongs to a recorded class.

    Without this, the record covers only the classes it already names and a new
    module could reintroduce the silence the record exists to end.
    """
    recorded = {reference.partition(":")[2] for reference in HIDDEN_CONSTRUCTOR_FIELDS}
    assigning: dict[str, str] = {}
    for path in sorted(SOURCE.rglob("*.py")):
        for match in re.finditer(
            r"^(\w+)\.__signature__ = ", path.read_text(encoding="utf-8"), re.MULTILINE
        ):
            assigning[match.group(1)] = path.relative_to(ROOT).as_posix()

    unrecorded = sorted(
        f"{name} ({where})" for name, where in assigning.items() if name not in recorded
    )

    assert unrecorded == [], (
        "these classes hide constructor fields from the protected-surface gate "
        f"without being recorded in HIDDEN_CONSTRUCTOR_FIELDS: {unrecorded}"
    )


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
