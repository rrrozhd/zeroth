"""Revocable project API keys for the hosted SDK boundary."""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column

from zeroth.econ.plane.database import Base
from zeroth.platform.storage.scoping import ResourceOperation, ResourceScopeDefinition

_ALL_OPERATIONS = frozenset(ResourceOperation)


class CloudApiKey(Base):
    __tablename__ = "cloud_api_keys"
    scope_definition: ClassVar[ResourceScopeDefinition] = ResourceScopeDefinition(
        resource_name="econ.cloud_api_key",
        table_name=__tablename__,
        operations=_ALL_OPERATIONS,
    )
    __table_args__ = (
        Index("uq_cloud_api_keys_secret_hash", "secret_hash", unique=True),
        Index("ix_cloud_api_keys_tenant_created", "tenant_id", "created_at"),
    )

    key_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    workspace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    last_four: Mapped[str] = mapped_column(String(4), nullable=False)
    roles_json: Mapped[list] = mapped_column(JSON, nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CloudSubscription(Base):
    __tablename__ = "cloud_subscriptions"
    scope_definition: ClassVar[ResourceScopeDefinition] = ResourceScopeDefinition(
        resource_name="econ.cloud_subscription",
        table_name=__tablename__,
        operations=_ALL_OPERATIONS,
    )

    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    plan: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    period_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    external_customer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    external_subscription_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    billing_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    external_price_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_billing_event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_billing_event_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class CloudUsageCounter(Base):
    __tablename__ = "cloud_usage_counters"
    scope_definition: ClassVar[ResourceScopeDefinition] = ResourceScopeDefinition(
        resource_name="econ.cloud_usage_counter",
        table_name=__tablename__,
        operations=_ALL_OPERATIONS,
    )

    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    period_start: Mapped[datetime] = mapped_column(DateTime, primary_key=True)
    meter: Mapped[str] = mapped_column(String(32), primary_key=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
