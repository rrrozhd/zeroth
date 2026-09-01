"""Durable normalized billing event receipts."""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from sqlalchemy import DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from zeroth.econ.plane.database import Base
from zeroth.platform.storage.scoping import ResourceOperation, ResourceScopeDefinition


class BillingEventReceipt(Base):
    __tablename__ = "billing_event_receipts"
    scope_definition: ClassVar[ResourceScopeDefinition] = ResourceScopeDefinition(
        resource_name="econ.billing_event_receipt",
        table_name=__tablename__,
        operations=frozenset(ResourceOperation),
    )
    __table_args__ = (
        Index(
            "ix_billing_receipts_tenant_subscription_time",
            "tenant_id",
            "external_subscription_id",
            "occurred_at",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    external_subscription_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    disposition: Mapped[str] = mapped_column(String(32), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    processed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
