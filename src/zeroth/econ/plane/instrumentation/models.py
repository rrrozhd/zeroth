from datetime import datetime
from decimal import Decimal
from typing import ClassVar

from sqlalchemy import DateTime, Index, Numeric, String, UniqueConstraint, text
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column

from zeroth.econ.plane.database import Base
from zeroth.platform.storage.scoping import ResourceOperation, ResourceScopeDefinition

_ALL_OPERATIONS = frozenset(ResourceOperation)


class ExecutionEvent(Base):
    __tablename__ = "execution_events"
    scope_definition: ClassVar[ResourceScopeDefinition] = ResourceScopeDefinition(
        resource_name="econ.execution_event", table_name=__tablename__, operations=_ALL_OPERATIONS
    )
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "execution_id",
            name="uq_execution_events_tenant_execution_id",
        ),
        Index(
            "ix_execution_events_tenant_time_capability",
            "tenant_id",
            "timestamp",
            "capability_id",
        ),
        Index("ix_execution_events_tenant_join_key", "tenant_id", "join_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    campaign_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    operation_id: Mapped[str | None] = mapped_column(String(192), index=True, nullable=True)
    deployment_ref: Mapped[str | None] = mapped_column(String(192), index=True, nullable=True)
    evidence_kind: Mapped[str] = mapped_column(String(32), default="legacy_unknown")
    provider_request_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    cleanup_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    execution_id: Mapped[str] = mapped_column(String(128), index=True)
    join_key: Mapped[str] = mapped_column(String(128), index=True, default="")
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)
    capability_id: Mapped[str] = mapped_column(String(128), index=True)
    implementation_id: Mapped[str] = mapped_column(String(128), index=True)
    model_version: Mapped[str] = mapped_column(String(128))
    token_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    tool_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    compute_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    cost_measurement: Mapped[str] = mapped_column(String(16), default="unmeasured")
    usage_measurement: Mapped[str] = mapped_column(String(16), default="unmeasured")
    latency_ms: Mapped[int] = mapped_column(default=0)
    compute_time_ms: Mapped[int] = mapped_column(default=0)
    event_metadata: Mapped[dict] = mapped_column("metadata", JSON, default=dict)


class OutcomeEvent(Base):
    __tablename__ = "outcome_events"
    scope_definition: ClassVar[ResourceScopeDefinition] = ResourceScopeDefinition(
        resource_name="econ.outcome_event", table_name=__tablename__, operations=_ALL_OPERATIONS
    )
    __table_args__ = (
        # Outcome identity.  ExecutionEvent carries one
        # (uq_execution_events_tenant_execution_id); without the equivalent here a
        # client that retries an ingest whose response it never saw silently
        # duplicates the outcome (A01-38).  The tuple is the identity the plane
        # already uses for an outcome elsewhere -- the connector outbox event_key
        # is f"{join_key}:{occurred_at}:{outcome_type}" (instrumentation/service.py)
        # -- widened by the attributed implementation.
        #
        # implementation_id is nullable and both SQLite and PostgreSQL treat NULLs
        # as distinct inside a unique key, which would exempt exactly the most
        # common batch shape (unlinked outcomes carry no implementation).  The
        # COALESCE expression collapses that hole; it costs reflection visibility
        # (SQLAlchemy skips expression-based indexes when reflecting) but a
        # constraint that does not fire on NULL would not be a constraint at all.
        Index(
            "uq_outcome_events_tenant_identity",
            "tenant_id",
            "join_key",
            "outcome_type",
            "occurred_at",
            text("coalesce(implementation_id, '')"),
            unique=True,
        ),
        Index(
            "ix_outcome_events_tenant_time_capability",
            "tenant_id",
            "occurred_at",
            "capability_id",
        ),
        Index("ix_outcome_events_tenant_join_key", "tenant_id", "join_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    join_key: Mapped[str] = mapped_column(String(128), index=True)
    execution_id: Mapped[str] = mapped_column(String(128), index=True, default="")
    capability_id: Mapped[str] = mapped_column(String(128), index=True)
    implementation_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    outcome_type: Mapped[str] = mapped_column(String(64))
    outcome_payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    outcome_value: Mapped[str] = mapped_column(String(255), default="")
    occurred_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    outcome_timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)
    provenance: Mapped[str] = mapped_column(String(16), default="MEASURED")


class EconErasureReceipt(Base):
    """Durable exactly-once receipt for tenant-scoped erasure operations."""

    __tablename__ = "econ_erasure_receipts"
    scope_definition: ClassVar[ResourceScopeDefinition] = ResourceScopeDefinition(
        resource_name="econ.erasure_receipt", table_name=__tablename__, operations=_ALL_OPERATIONS
    )

    operation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    deleted_count: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
