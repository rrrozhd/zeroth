from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from zeroth.econ.plane.billing.models import BillingEventReceipt
from zeroth.econ.plane.billing.schemas import BillingSubscriptionEvent
from zeroth.econ.plane.billing.service import BillingEventConflict, apply_subscription_event
from zeroth.econ.plane.cloud.entitlements import EntitlementError, reserve_usage
from zeroth.econ.plane.cloud.models import CloudSubscription
from zeroth.econ.plane.config import settings
from zeroth.econ.plane.database import Base
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext


def _event(**changes: object) -> BillingSubscriptionEvent:
    occurred_at = datetime(2026, 8, 31, 12, tzinfo=UTC)
    payload: dict[str, object] = {
        "provider": "test-mor",
        "event_id": "evt-1",
        "tenant_id": "tenant-a",
        "external_customer_id": "customer-1",
        "external_subscription_id": "subscription-1",
        "external_price_id": "price-solo",
        "plan": "solo",
        "status": "active",
        "period_start": occurred_at,
        "period_end": occurred_at + timedelta(days=30),
        "occurred_at": occurred_at,
    }
    payload.update(changes)
    return BillingSubscriptionEvent.model_validate(payload)


def test_newer_billing_event_projects_the_local_subscription(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'billing.db'}")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        db = ScopedSession(session, TenantWideScopeContext(tenant_id="tenant-a"))
        result = apply_subscription_event(db, _event())

        subscription = db.get(CloudSubscription, "tenant-a")
        assert result.disposition == "applied"
        assert subscription is not None
        assert subscription.plan == "solo"
        assert subscription.status == "active"
        assert subscription.billing_provider == "test-mor"
        assert subscription.last_billing_event_id == "evt-1"


def test_exact_billing_event_retry_is_idempotent(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'duplicate.db'}")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        db = ScopedSession(session, TenantWideScopeContext(tenant_id="tenant-a"))
        apply_subscription_event(db, _event())
        repeated = apply_subscription_event(db, _event())

        assert repeated.disposition == "duplicate"
        assert len(list(db.scalars(select(BillingEventReceipt)))) == 1


def test_changed_replay_of_a_billing_event_is_a_conflict(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'conflict.db'}")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        db = ScopedSession(session, TenantWideScopeContext(tenant_id="tenant-a"))
        apply_subscription_event(db, _event())

        with pytest.raises(BillingEventConflict, match="payload changed"):
            apply_subscription_event(db, _event(status="canceled"))


def test_older_event_is_retained_without_rolling_access_backward(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'stale.db'}")
    Base.metadata.create_all(engine)
    canceled_at = datetime(2026, 9, 1, 12, tzinfo=UTC)

    with Session(engine) as session:
        db = ScopedSession(session, TenantWideScopeContext(tenant_id="tenant-a"))
        applied = apply_subscription_event(
            db,
            _event(event_id="evt-new", status="canceled", occurred_at=canceled_at),
        )
        stale = apply_subscription_event(
            db,
            _event(event_id="evt-old", status="active", occurred_at=canceled_at - timedelta(days=1)),
        )

        subscription = db.get(CloudSubscription, "tenant-a")
        receipt = db.get(BillingEventReceipt, ("tenant-a", "test-mor", "evt-old"))
        assert applied.disposition == "applied"
        assert stale.disposition == "ignored_stale"
        assert subscription is not None and subscription.status == "canceled"
        assert subscription.last_billing_event_id == "evt-new"
        assert receipt is not None and receipt.disposition == "ignored_stale"


def test_equal_timestamp_event_is_retained_without_reordering_projection(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'equal-time.db'}")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        db = ScopedSession(session, TenantWideScopeContext(tenant_id="tenant-a"))
        first = apply_subscription_event(db, _event(event_id="evt-first"))
        ambiguous = apply_subscription_event(
            db,
            _event(event_id="evt-second", status="canceled"),
        )

        subscription = db.get(CloudSubscription, "tenant-a")
        assert first.disposition == "applied"
        assert ambiguous.disposition == "ignored_stale"
        assert subscription is not None and subscription.status == "active"
        assert subscription.last_billing_event_id == "evt-first"


def test_billing_event_must_match_the_bound_tenant(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'tenant.db'}")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        db = ScopedSession(session, TenantWideScopeContext(tenant_id="tenant-b"))
        with pytest.raises(ValueError, match="tenant does not match"):
            apply_subscription_event(db, _event())

    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM cloud_subscriptions")).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM billing_event_receipts")).scalar_one() == 0


def test_provider_takeover_requires_an_explicit_migration(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'provider.db'}")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        db = ScopedSession(session, TenantWideScopeContext(tenant_id="tenant-a"))
        apply_subscription_event(db, _event())

        with pytest.raises(ValueError, match="provider does not match"):
            apply_subscription_event(db, _event(provider="another-mor", event_id="evt-2"))


@pytest.mark.parametrize("status", ["past_due", "paused", "canceled"])
def test_non_current_billing_status_revokes_hosted_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / f'{status}.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(settings, "cloud_entitlements_enabled", True)

    with Session(engine) as session:
        db = ScopedSession(session, TenantWideScopeContext(tenant_id="tenant-a"))
        apply_subscription_event(db, _event(status=status))

        with pytest.raises(EntitlementError, match="active Zeroth Cloud subscription required"):
            reserve_usage(db, "events")
