"""The decision response must say when the decision enacted nothing.

A01-11's fix made ``decide_action`` *skip* -- rather than mis-transition -- the
policy row of a legacy enforcement action whose ``enforcement_action_id`` link is
NULL.  The skip is deliberate: refusing would make pre-link actions impossible to
even *reject*, and rejection is the safety operation.

But the skip was invisible at the boundary.  An approver clearing a legacy
``AdjustTrafficWeights`` action got ``200 {"status": "approved"}`` while the
weights were never written, and the only record was an ``audit_log`` row nobody
reads in the moment.  Quiet is better than the old wrong behaviour; it should not
be silent.

These tests pin ``policy_action_effect`` on the response.  They pin *reporting*
only -- the audited-skip semantics, the status codes and the absence of an error
path are asserted to be unchanged.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from zeroth.econ.plane.auth.deps import get_current_scoped_db, get_current_user
from zeroth.econ.plane.auth.scoped import ScopedUserClaims

# Model modules whose tables must exist before ``create_all`` runs.
from zeroth.econ.plane.capabilities import models as capability_models  # noqa: F401
from zeroth.econ.plane.connectors import models as connector_models  # noqa: F401
from zeroth.econ.plane.counterfactual import (
    models as counterfactual_models,  # noqa: F401
)
from zeroth.econ.plane.database import Base
from zeroth.econ.plane.enforcement.api import router as enforcement_router
from zeroth.econ.plane.enforcement.models import (
    EnforcementAction,
    PolicyAction,
    TrafficPolicy,
)
from zeroth.econ.plane.enforcement.schemas import EnforcementActionOut
from zeroth.econ.plane.instrumentation import (
    models as instrumentation_models,  # noqa: F401
)
from zeroth.econ.plane.performance import models as performance_models  # noqa: F401
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext

_TENANT = "tenant-a"
_APPROVER = "approver@example.com"


@pytest.fixture(autouse=True)
def _connectors_disabled(monkeypatch) -> None:
    """Keep the outbox out of the picture; only the response shape is under test."""
    monkeypatch.setattr("zeroth.econ.plane.enforcement.service.settings.connectors_enabled", False)


@pytest.fixture
def econ_engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def client(econ_engine) -> TestClient:
    """The enforcement router bound to ``tenant-a`` as an Approver."""
    app = FastAPI()
    app.include_router(enforcement_router, prefix="/v1")

    def scoped_db():
        with Session(econ_engine) as db:
            yield ScopedSession(db, TenantWideScopeContext(tenant_id=_TENANT))

    app.dependency_overrides[get_current_scoped_db] = scoped_db
    app.dependency_overrides[get_current_user] = lambda: ScopedUserClaims(
        sub=_APPROVER,
        email=_APPROVER,
        roles=["Approver", "Admin"],
        tenant_id=_TENANT,
        exp=2_000_000_000,
        iss="test",
    )
    return TestClient(app)


def _create_linked_action(client: TestClient, action_type: str = "AdjustTrafficWeights") -> dict:
    """Create through the API so the enforcement->policy link is established."""
    response = client.post(
        "/v1/enforcement/actions",
        json={
            "capability_id": "cap-a",
            "action_type": action_type,
            "reason": "linked",
            "before_config": {"impl-a": 50},
            "after_config": {"impl-a": 100},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _seed_unlinked_legacy_action(engine, *, action_type: str = "AdjustTrafficWeights") -> int:
    """Persist the pre-link shape: an action and a policy row with no link between them."""
    with Session(engine) as seed:
        action = EnforcementAction(
            tenant_id=_TENANT,
            capability_id="cap-a",
            action_type=action_type,
            status="pending",
            reason="legacy",
            before_config={"impl-a": 50},
            after_config={"impl-a": 100},
            created_at=datetime.now(UTC),
        )
        seed.add(action)
        seed.add(
            PolicyAction(
                tenant_id=_TENANT,
                capability_id="cap-a",
                enforcement_action_id=None,
                proposed_at=datetime.now(UTC),
                proposed_by="system",
                action_type=action_type,
                payload_json={},
                confidence_state_json={},
                status="PROPOSED",
            )
        )
        seed.commit()
        return action.id


# ---------------------------------------------------------------------------
# The four decision x linkage cases.  ``policy_action_effect`` has to read
# correctly in every one of them, not just in the case that motivated it.
# ---------------------------------------------------------------------------


def test_approving_an_unlinked_legacy_action_reports_that_nothing_was_applied(
    client, econ_engine
) -> None:
    """The motivating case: 200 approved, traffic weights never written."""
    action_id = _seed_unlinked_legacy_action(econ_engine)

    response = client.post(f"/v1/enforcement/actions/{action_id}/approve", json={"reason": "ok"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "approved"
    assert body["policy_action_effect"] == "not_applied"
    # The report must match reality: nothing was enacted.
    with Session(econ_engine) as check:
        assert check.scalars(select(TrafficPolicy)).all() == []
        assert check.scalars(select(PolicyAction)).one().status == "PROPOSED"


def test_rejecting_an_unlinked_legacy_action_reports_that_nothing_was_applied(
    client, econ_engine
) -> None:
    action_id = _seed_unlinked_legacy_action(econ_engine)

    response = client.post(f"/v1/enforcement/actions/{action_id}/reject", json={"reason": "no"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "rejected"
    assert body["policy_action_effect"] == "not_applied"
    with Session(econ_engine) as check:
        assert check.scalars(select(PolicyAction)).one().status == "PROPOSED"


def test_approving_a_linked_action_reports_the_decision_reached_the_policy_action(
    client, econ_engine
) -> None:
    action = _create_linked_action(client)

    response = client.post(f"/v1/enforcement/actions/{action['id']}/approve", json={"reason": "ok"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "approved"
    assert body["policy_action_effect"] == "applied"
    with Session(econ_engine) as check:
        assert check.scalars(select(PolicyAction)).one().status == "APPLIED"


def test_rejecting_a_linked_action_reports_the_decision_reached_the_policy_action(
    client, econ_engine
) -> None:
    """A linked rejection applies the *decision* to the policy action (-> REJECTED).

    ``applied`` names the decision reaching a policy action, not the traffic
    weights being written -- otherwise a linked rejection, which enacts nothing
    by design, would be indistinguishable from a legacy skip that enacts nothing
    by accident.
    """
    action = _create_linked_action(client)

    response = client.post(f"/v1/enforcement/actions/{action['id']}/reject", json={"reason": "no"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "rejected"
    assert body["policy_action_effect"] == "applied"
    with Session(econ_engine) as check:
        assert check.scalars(select(PolicyAction)).one().status == "REJECTED"


# ---------------------------------------------------------------------------
# ``null`` must not double as "unlinked"
# ---------------------------------------------------------------------------


def test_create_and_list_report_no_decision_outcome_rather_than_not_applied(client) -> None:
    """``null`` means "this response reports no decision", never "nothing applied".

    Overloading ``null`` would leave a client unable to tell a non-decision
    endpoint from an approval that enacted nothing -- reintroducing exactly the
    ambiguity this field exists to remove.
    """
    created = _create_linked_action(client)
    assert created["policy_action_effect"] is None

    listed = client.get("/v1/enforcement/actions")
    assert listed.status_code == 200, listed.text
    assert [row["policy_action_effect"] for row in listed.json()] == [None]


def test_the_unlinked_signal_is_an_explicit_value_not_an_omitted_key(client, econ_engine) -> None:
    """Serialised responses must carry the key, so clients cannot miss the signal."""
    action_id = _seed_unlinked_legacy_action(econ_engine)

    body = client.post(f"/v1/enforcement/actions/{action_id}/approve", json={"reason": "ok"}).json()

    assert "policy_action_effect" in body
    assert body["policy_action_effect"] is not None


# ---------------------------------------------------------------------------
# Reporting only: no status-code or error-path change
# ---------------------------------------------------------------------------


def test_the_reporting_field_introduces_no_error_path_for_unlinked_actions(
    client, econ_engine
) -> None:
    """An unlinked decision still succeeds; only a missing action is a 404."""
    action_id = _seed_unlinked_legacy_action(econ_engine, action_type="TriggerInvestigation")

    decided = client.post(f"/v1/enforcement/actions/{action_id}/approve", json={"reason": "ok"})
    missing = client.post("/v1/enforcement/actions/999999/approve", json={"reason": "ok"})

    assert decided.status_code == 200
    assert (missing.status_code, missing.json()) == (404, {"detail": "Action not found"})


def test_the_protected_surface_exclusion_narrows_the_signature_and_nothing_else() -> None:
    """The pinned ``__init__`` signature hides the field; the model does not.

    ``EnforcementActionOut.__init__`` is pinned by the protected-surface fixtures,
    and the legacy half of that pin is immutable -- so the additive field is
    excluded from the introspected signature, exactly as ``ServiceBootstrap``
    excludes its additive components.  This test is the counterweight: it refuses
    to let that exclusion turn into a way to drop the field, by proving the field
    is still real, still keyword-constructible, and still defaults to ``None``.
    """
    assert "policy_action_effect" not in inspect.signature(EnforcementActionOut).parameters

    field = EnforcementActionOut.model_fields["policy_action_effect"]
    assert field.default is None

    decided = EnforcementActionOut(
        id=1,
        capability_id="cap-a",
        action_type="AdjustTrafficWeights",
        status="approved",
        reason="legacy",
        before_config={},
        after_config={},
        approver_sub=_APPROVER,
        approved_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
        policy_action_effect="not_applied",
    )
    assert decided.policy_action_effect == "not_applied"
    assert decided.model_dump()["policy_action_effect"] == "not_applied"


def test_the_field_is_documented_and_optional_in_the_published_schema(client) -> None:
    """Additive and optional, so existing clients keep validating.

    Every pre-existing property must stay required: an additive change that
    relaxed one of them would break clients quietly, which is the failure mode
    this whole package is about.
    """
    schema = client.app.openapi()["components"]["schemas"]["EnforcementActionOut"]
    field = schema["properties"]["policy_action_effect"]

    assert set(schema["required"]) == {
        "id",
        "capability_id",
        "action_type",
        "status",
        "reason",
        "before_config",
        "after_config",
        "approver_sub",
        "approved_at",
        "created_at",
    }
    assert {"type": "null"} in field["anyOf"]
    assert sorted(value for branch in field["anyOf"] for value in branch.get("enum", ())) == [
        "applied",
        "not_applied",
    ]
    # The audited-skip semantics are only legible if the value is explained.
    assert "not_applied" in field["description"]
