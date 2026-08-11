"""Wire-contract tests for the ZER-8 decision request/response pair (R1).

**R1 -- the wire types are versioned.** ``DecisionRequest`` and
``DecisionResponse`` both carry ``schema_version: Literal[1]``, and both forbid
extras. Together that makes a version bump a *different* wire type rather than a
field a caller can set: a v2 request cannot be validated as a v1 one, and --
because ``request_digest`` covers ``schema_version`` -- cannot replay a v1
decision either.

The field-set assertions below are the other half. A ``Literal[1]`` field that
somebody deleted would make every version assertion here vacuously absent rather
than failing, so the models' field sets are pinned explicitly: removing
``tenant_id`` from the request, or ``fingerprint`` from the action, is a test
failure here even though nothing else in this file would notice.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from zeroth.governance.decisions import (
    DecisionKind,
    DecisionRequest,
    DecisionResponse,
    NormalizedAction,
)

POLICY_VERSION = f"sha256:{'a' * 64}"
ISSUED_AT = datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC)


def request_fields(**overrides: Any) -> dict[str, Any]:
    """Return constructor kwargs for a complete ``DecisionRequest``."""
    fields: dict[str, Any] = {
        "tenant_id": "tenant-alpha",
        "principal_id": "principal-alpha",
        "deployment_ref": "dep-alpha",
        "action": NormalizedAction(
            name="send_email",
            fingerprint=f"sha256:{'1' * 64}",
            arguments_digest=f"sha256:{'2' * 64}",
            contract_ref="contracts/email@v1",
            side_effect="side_effecting",
        ),
        "idempotency_key": "key-alpha",
        "policy_bindings": ("binding-a",),
    }
    fields.update(overrides)
    return fields


def response_fields(**overrides: Any) -> dict[str, Any]:
    """Return constructor kwargs for a complete ``DecisionResponse``."""
    fields: dict[str, Any] = {
        "decision_id": "decision-alpha",
        "kind": DecisionKind.ALLOW,
        "reason_code": "unknown_error",
        "approval_ref": None,
        "policy_version": POLICY_VERSION,
        "tenant_id": "tenant-alpha",
        "issued_at": ISSUED_AT,
    }
    fields.update(overrides)
    return fields


def test_the_request_and_response_are_pinned_to_schema_version_one() -> None:
    """Both wire types default to -- and report -- version 1, not merely accept it."""
    request = DecisionRequest(**request_fields())
    response = DecisionResponse(**response_fields())

    assert request.schema_version == 1
    assert response.schema_version == 1
    # Pinned on the wire, not just in memory: a version the serializer drops is
    # a version the receiving side cannot reject.
    assert request.model_dump(mode="json")["schema_version"] == 1
    assert response.model_dump(mode="json")["schema_version"] == 1
    # Stating it explicitly is what makes a bump to Literal[2] fail here rather
    # than silently redefine what "version 1" means.
    assert DecisionRequest(**request_fields(schema_version=1)).schema_version == 1
    assert DecisionResponse(**response_fields(schema_version=1)).schema_version == 1


@pytest.mark.parametrize("version", [0, 2, "1", None])
def test_a_foreign_schema_version_is_rejected(version: Any) -> None:
    """A request or response claiming another version is not a v1 message.

    ``Literal[1]`` plus ``extra="forbid"`` is what makes this a rejection rather
    than a coerced or ignored field: a v2 sender must fail loudly instead of
    being read as v1 by a server that never learned the new shape.
    """
    with pytest.raises(ValidationError):
        DecisionRequest(**request_fields(schema_version=version))
    with pytest.raises(ValidationError):
        DecisionResponse(**response_fields(schema_version=version))


def test_the_response_carries_the_policy_version_and_tenant_it_was_decided_for() -> None:
    """A verdict without its policy revision and tenant is not audit evidence.

    Both travel on the response so a stored decision can be re-read later and
    still say *which* policy admitted it and *whose* call it was -- neither is
    recoverable from ``kind`` and ``decision_id`` alone.
    """
    response = DecisionResponse(
        **response_fields(policy_version=POLICY_VERSION, tenant_id="tenant-beta")
    )

    assert response.policy_version == POLICY_VERSION
    assert response.tenant_id == "tenant-beta"
    assert response.issued_at == ISSUED_AT
    dumped = response.model_dump(mode="json")
    assert dumped["policy_version"] == POLICY_VERSION
    assert dumped["tenant_id"] == "tenant-beta"


def test_the_request_carries_every_field_a_decision_is_attributed_to() -> None:
    """Removing any of these fields would drop what a decision is bound to.

    ``request_digest`` digests the request by dumping it, so a deleted field is
    a field that silently stops binding the decision. Pinned as a superset check
    rather than equality so adding a field stays a non-breaking change.
    """
    required = {
        "schema_version",
        "tenant_id",
        "principal_id",
        "deployment_ref",
        "action",
        "idempotency_key",
        "policy_bindings",
    }

    assert required <= set(DecisionRequest.model_fields)


def test_the_normalized_action_carries_the_fields_a_policy_is_written_against() -> None:
    """The action's identity is the fingerprint, not the caller-chosen name."""
    required = {
        "name",
        "fingerprint",
        "arguments_digest",
        "contract_ref",
        "side_effect",
        "capability_refs",
        "requires_approval",
    }

    assert required <= set(NormalizedAction.model_fields)


def test_the_response_carries_every_field_a_verdict_is_read_from() -> None:
    """A response missing any of these could not be re-served as evidence."""
    required = {
        "schema_version",
        "decision_id",
        "kind",
        "reason_code",
        "approval_ref",
        "policy_version",
        "tenant_id",
        "issued_at",
    }

    assert required <= set(DecisionResponse.model_fields)
