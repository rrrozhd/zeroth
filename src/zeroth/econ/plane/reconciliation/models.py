from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import ClassVar

from sqlalchemy import DateTime, ForeignKeyConstraint, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column

from zeroth.econ.plane.database import Base
from zeroth.platform.storage.scoping import ResourceOperation, ResourceScopeDefinition

_ALL_OPERATIONS = frozenset(ResourceOperation)


class ProviderBill(Base):
    """Immutable normalized provider cost statement."""

    __tablename__ = "provider_bills"
    scope_definition: ClassVar[ResourceScopeDefinition] = ResourceScopeDefinition(
        resource_name="econ.provider_bill",
        table_name=__tablename__,
        # Scope every ORM operation even though the public bill API is append-only.
        # This inventory drives tenant-isolation probes; it is not an API permission list.
        operations=_ALL_OPERATIONS,
    )
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "id", name="uq_provider_bills_tenant_id"
        ),
        UniqueConstraint(
            "tenant_id",
            "provider",
            "statement_id",
            name="uq_provider_bills_tenant_provider_statement",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    statement_id: Mapped[str] = mapped_column(String(192), index=True, nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    billed_total_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    statement_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    imported_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ProviderCostBucket(Base):
    """One provider cost bucket within an immutable statement."""

    __tablename__ = "provider_cost_buckets"
    scope_definition: ClassVar[ResourceScopeDefinition] = ResourceScopeDefinition(
        resource_name="econ.provider_cost_bucket",
        table_name=__tablename__,
        operations=_ALL_OPERATIONS,
    )
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "provider_bill_id"],
            ["provider_bills.tenant_id", "provider_bills.id"],
            name="fk_provider_cost_buckets_tenant_bill",
        ),
        UniqueConstraint(
            "tenant_id",
            "provider_bill_id",
            "bucket_id",
            name="uq_provider_cost_buckets_tenant_bill_bucket",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    provider_bill_id: Mapped[int] = mapped_column(index=True, nullable=False)
    bucket_id: Mapped[str] = mapped_column(String(192), nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_dimensions: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
