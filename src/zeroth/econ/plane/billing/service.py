"""Project normalized billing events into local entitlement state."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import hmac
import json

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from zeroth.econ.plane.billing.models import BillingEventReceipt
from zeroth.econ.plane.billing.schemas import BillingSubscriptionEvent, BillingSyncResult
from zeroth.econ.plane.cloud.models import CloudSubscription
from zeroth.econ.plane.config import settings
from zeroth.econ.plane.scoped_session import ScopedSession


class BillingEventConflict(RuntimeError):
    """An event identity was replayed with different normalized content."""


def _digest(event: BillingSubscriptionEvent) -> str:
    payload = json.dumps(
        event.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return hmac.new(settings.jwt_secret.encode(), payload, hashlib.sha256).hexdigest()


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def apply_subscription_event(
    db: ScopedSession,
    event: BillingSubscriptionEvent,
    *,
    _retry_on_race: bool = True,
) -> BillingSyncResult:
    if type(db) is not ScopedSession or db.scope is None:
        raise TypeError("billing synchronization requires a tenant-scoped session")
    if event.tenant_id != db.scope.tenant_id:
        raise ValueError("billing event tenant does not match the bound scope")
    payload_digest = _digest(event)
    identity = (event.tenant_id, event.provider, event.event_id)
    prior = db.get(BillingEventReceipt, identity)
    if prior is not None:
        if prior.payload_digest != payload_digest:
            raise BillingEventConflict("billing event payload changed for an existing identity")
        return BillingSyncResult(
            disposition="duplicate",
            provider=event.provider,
            event_id=event.event_id,
            tenant_id=event.tenant_id,
        )

    subscription = db.scalars(
        select(CloudSubscription)
        .where(CloudSubscription.tenant_id == event.tenant_id)
        .with_for_update()
    ).one_or_none()
    now = datetime.now(UTC)
    if subscription is not None and subscription.billing_provider not in {None, event.provider}:
        raise ValueError("billing provider does not match the existing subscription")
    last_event_at = (
        _utc(subscription.last_billing_event_at)
        if subscription is not None and subscription.last_billing_event_at is not None
        else None
    )
    disposition = (
        "ignored_stale"
        if last_event_at is not None and _utc(event.occurred_at) <= last_event_at
        else "applied"
    )
    db.add(
        BillingEventReceipt(
            tenant_id=event.tenant_id,
            provider=event.provider,
            event_id=event.event_id,
            external_subscription_id=event.external_subscription_id,
            payload_digest=payload_digest,
            disposition=disposition,
            occurred_at=event.occurred_at,
            processed_at=now,
        )
    )
    if disposition == "ignored_stale":
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            if not _retry_on_race:
                raise
            return apply_subscription_event(db, event, _retry_on_race=False)
        return BillingSyncResult(
            disposition="ignored_stale",
            provider=event.provider,
            event_id=event.event_id,
            tenant_id=event.tenant_id,
        )

    if subscription is None:
        subscription = CloudSubscription(
            tenant_id=event.tenant_id,
            plan=event.plan,
            status=event.status,
            period_start=event.period_start,
            period_end=event.period_end,
            external_customer_id=event.external_customer_id,
            external_subscription_id=event.external_subscription_id,
            billing_provider=event.provider,
            external_price_id=event.external_price_id,
            last_billing_event_id=event.event_id,
            last_billing_event_at=event.occurred_at,
            updated_at=now,
        )
        db.add(subscription)
    else:
        subscription.plan = event.plan
        subscription.status = event.status
        subscription.period_start = event.period_start
        subscription.period_end = event.period_end
        subscription.external_customer_id = event.external_customer_id
        subscription.external_subscription_id = event.external_subscription_id
        subscription.billing_provider = event.provider
        subscription.external_price_id = event.external_price_id
        subscription.last_billing_event_id = event.event_id
        subscription.last_billing_event_at = event.occurred_at
        subscription.updated_at = now
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if not _retry_on_race:
            raise
        return apply_subscription_event(db, event, _retry_on_race=False)
    return BillingSyncResult(
        disposition=disposition,
        provider=event.provider,
        event_id=event.event_id,
        tenant_id=event.tenant_id,
    )
