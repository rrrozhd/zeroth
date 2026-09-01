"""Idempotent local activation from a verified WorkOS identity."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from zeroth.econ.plane.cloud.keys_schemas import ApiKeyCreate
from zeroth.econ.plane.cloud.keys_service import _new_api_key
from zeroth.econ.plane.cloud.models import (
    CloudApiKey,
    CloudIdentityMembership,
    CloudSubscription,
    CloudTenantBinding,
)

_PROVIDER = "workos"
_TRIAL_DAYS = 14
_QUICKSTART_KEY_NAME = "quickstart"


@dataclass(frozen=True)
class VerifiedWorkOSIdentity:
    external_user_id: str
    external_organization_id: str
    email: str
    email_verified: bool


@dataclass(frozen=True)
class TrialActivation:
    tenant_id: str
    key_id: str
    api_key: str | None
    created: bool


def _tenant_id(external_organization_id: str) -> str:
    identity = f"zeroth:{_PROVIDER}:{external_organization_id}"
    return f"ten_{uuid.uuid5(uuid.NAMESPACE_URL, identity).hex[:24]}"


def activate_trial(
    session: Session,
    identity: VerifiedWorkOSIdentity,
    *,
    now: datetime | None = None,
    _retry_on_race: bool = True,
) -> TrialActivation:
    """Create the local tenant, membership, trial, and first key exactly once."""

    if not identity.email_verified:
        raise ValueError("verified WorkOS email required")
    if not all(
        value.strip()
        for value in (
            identity.external_user_id,
            identity.external_organization_id,
            identity.email,
        )
    ):
        raise ValueError("WorkOS identity fields must not be empty")
    timestamp = now or datetime.now(UTC)
    binding = session.scalar(
        select(CloudTenantBinding).where(
            CloudTenantBinding.provider == _PROVIDER,
            CloudTenantBinding.external_organization_id
            == identity.external_organization_id,
        )
    )
    created = binding is None
    if binding is None:
        binding = CloudTenantBinding(
            local_tenant_id=_tenant_id(identity.external_organization_id),
            provider=_PROVIDER,
            external_organization_id=identity.external_organization_id,
            created_at=timestamp,
        )
        session.add(binding)

    membership_id = (binding.local_tenant_id, _PROVIDER, identity.external_user_id)
    membership = session.get(CloudIdentityMembership, membership_id)
    if membership is None:
        session.add(
            CloudIdentityMembership(
                tenant_id=binding.local_tenant_id,
                provider=_PROVIDER,
                external_user_id=identity.external_user_id,
                external_organization_id=identity.external_organization_id,
                email=identity.email,
                created_at=timestamp,
                updated_at=timestamp,
            )
        )
    elif membership.email != identity.email:
        membership.email = identity.email
        membership.updated_at = timestamp

    subscription = session.get(CloudSubscription, binding.local_tenant_id)
    if subscription is None:
        session.add(
            CloudSubscription(
                tenant_id=binding.local_tenant_id,
                plan="trial",
                status="trialing",
                period_start=timestamp,
                period_end=timestamp + timedelta(days=_TRIAL_DAYS),
                external_customer_id=None,
                external_subscription_id=None,
                billing_provider=None,
                external_price_id=None,
                last_billing_event_id=None,
                last_billing_event_at=None,
                updated_at=timestamp,
            )
        )

    key = session.scalar(
        select(CloudApiKey).where(
            CloudApiKey.tenant_id == binding.local_tenant_id,
            CloudApiKey.name == _QUICKSTART_KEY_NAME,
            CloudApiKey.revoked_at.is_(None),
        )
    )
    api_key: str | None = None
    if key is None:
        key, reveal = _new_api_key(
            tenant_id=binding.local_tenant_id,
            payload=ApiKeyCreate(name=_QUICKSTART_KEY_NAME, roles=["Analyst"]),
            subject=f"workos:{identity.external_user_id}",
            workspace_id=None,
            now=timestamp,
        )
        session.add(key)
        api_key = reveal.api_key
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        if not _retry_on_race:
            raise
        return activate_trial(
            session,
            identity,
            now=timestamp,
            _retry_on_race=False,
        )
    return TrialActivation(
        tenant_id=binding.local_tenant_id,
        key_id=key.key_id,
        api_key=api_key,
        created=created,
    )


__all__ = [
    "CloudIdentityMembership",
    "CloudTenantBinding",
    "TrialActivation",
    "VerifiedWorkOSIdentity",
    "activate_trial",
]
