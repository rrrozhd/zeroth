from __future__ import annotations

from datetime import UTC, datetime, timedelta
import importlib
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from zeroth.econ.plane.cloud.models import CloudApiKey, CloudSubscription
from zeroth.econ.plane.database import Base


def _activation_api():
    try:
        return importlib.import_module("zeroth.econ.plane.cloud.activation")
    except ModuleNotFoundError:
        pytest.fail("cloud trial activation service is not implemented")


def _identity(**changes: object):
    activation = _activation_api()
    payload: dict[str, object] = {
        "external_user_id": "user_01",
        "external_organization_id": "org_01",
        "email": "owner@example.com",
        "email_verified": True,
    }
    payload.update(changes)
    return activation.VerifiedWorkOSIdentity(**payload)


def test_verified_workos_organization_activates_one_trial_and_project_key(
    tmp_path: Path,
) -> None:
    activation = _activation_api()
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'activation.db'}")
    Base.metadata.create_all(engine)
    now = datetime(2026, 8, 31, 15, tzinfo=UTC)

    with Session(engine) as session:
        result = activation.activate_trial(session, _identity(), now=now)

        binding = session.scalar(select(activation.CloudTenantBinding))
        membership = session.scalar(select(activation.CloudIdentityMembership))
        subscription = session.get(CloudSubscription, result.tenant_id)
        key = session.get(CloudApiKey, result.key_id)

        assert result.created is True
        assert result.api_key is not None and result.api_key.startswith("zth_live_")
        assert binding.external_organization_id == "org_01"
        assert binding.local_tenant_id == result.tenant_id
        assert membership.external_user_id == "user_01"
        assert membership.email == "owner@example.com"
        assert subscription is not None
        assert subscription.plan == "trial"
        assert subscription.status == "trialing"
        assert subscription.period_start == now.replace(tzinfo=None)
        assert subscription.period_end == (now + timedelta(days=14)).replace(tzinfo=None)
        assert key is not None and key.roles_json == ["Analyst"]


def test_exact_activation_retry_does_not_issue_another_key_or_restart_trial(
    tmp_path: Path,
) -> None:
    activation = _activation_api()
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'retry.db'}")
    Base.metadata.create_all(engine)
    first_at = datetime(2026, 8, 31, 15, tzinfo=UTC)

    with Session(engine) as session:
        first = activation.activate_trial(session, _identity(), now=first_at)
        repeated = activation.activate_trial(
            session,
            _identity(),
            now=first_at + timedelta(days=2),
        )

        subscription = session.get(CloudSubscription, first.tenant_id)
        key_count = session.scalar(select(func.count()).select_from(CloudApiKey))
        membership_count = session.scalar(
            select(func.count()).select_from(activation.CloudIdentityMembership)
        )

        assert repeated.created is False
        assert repeated.api_key is None
        assert repeated.key_id == first.key_id
        assert key_count == 1
        assert membership_count == 1
        assert subscription is not None
        assert subscription.period_start == first_at.replace(tzinfo=None)


def test_second_verified_user_joins_the_same_tenant_without_receiving_shared_key(
    tmp_path: Path,
) -> None:
    activation = _activation_api()
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'member.db'}")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        owner = activation.activate_trial(session, _identity())
        member = activation.activate_trial(
            session,
            _identity(external_user_id="user_02", email="member@example.com"),
        )

        memberships = list(session.scalars(select(activation.CloudIdentityMembership)))
        assert member.tenant_id == owner.tenant_id
        assert member.created is False
        assert member.api_key is None
        assert {row.external_user_id for row in memberships} == {"user_01", "user_02"}
        assert session.scalar(select(func.count()).select_from(CloudApiKey)) == 1


def test_unverified_workos_email_cannot_create_tenant_state(tmp_path: Path) -> None:
    activation = _activation_api()
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'unverified.db'}")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        with pytest.raises(ValueError, match="verified WorkOS email required"):
            activation.activate_trial(session, _identity(email_verified=False))

        assert session.scalar(select(func.count()).select_from(CloudSubscription)) == 0
        assert session.scalar(select(func.count()).select_from(CloudApiKey)) == 0


def test_activation_never_downgrades_an_existing_paid_subscription(tmp_path: Path) -> None:
    activation = _activation_api()
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'paid.db'}")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)

    with Session(engine) as session:
        first = activation.activate_trial(session, _identity(), now=now)
        subscription = session.get(CloudSubscription, first.tenant_id)
        assert subscription is not None
        subscription.plan = "solo"
        subscription.status = "active"
        subscription.period_end = now + timedelta(days=30)
        session.commit()

        activation.activate_trial(session, _identity(), now=now + timedelta(days=1))

        persisted = session.get(CloudSubscription, first.tenant_id)
        assert persisted is not None
        assert persisted.plan == "solo"
        assert persisted.status == "active"


def test_concurrent_org_callback_converges_on_existing_activation(tmp_path: Path) -> None:
    activation = _activation_api()
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'race.db'}")
    Base.metadata.create_all(engine)

    class RacingSession(Session):
        raced = False

        def commit(self) -> None:
            if not self.raced:
                self.raced = True
                super().rollback()
                with Session(engine) as winner:
                    activation.activate_trial(winner, _identity())
                raise IntegrityError("concurrent activation", {}, RuntimeError("unique"))
            super().commit()

    with RacingSession(engine) as session:
        result = activation.activate_trial(session, _identity())

        assert result.created is False
        assert result.api_key is None
        assert session.scalar(select(func.count()).select_from(CloudApiKey)) == 1
