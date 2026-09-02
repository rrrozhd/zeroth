from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from sqlalchemy import DateTime, Index, LargeBinary, String, UniqueConstraint
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column

from zeroth.econ.plane.database import Base
from zeroth.platform.storage.scoping import ResourceOperation, ResourceScopeDefinition

_ALL_OPERATIONS = frozenset(ResourceOperation)


class DecisionReportRecord(Base):
    """A byte-stable rendering of one immutable decision."""

    __tablename__ = "decision_reports"
    scope_definition: ClassVar[ResourceScopeDefinition] = ResourceScopeDefinition(
        resource_name="econ.decision_report",
        table_name=__tablename__,
        operations=_ALL_OPERATIONS,
    )
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "decision_id", "template_version", name="uq_decision_report_version"
        ),
        Index("ix_decision_reports_tenant_created", "tenant_id", "created_at"),
    )

    report_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    decision_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    template_version: Mapped[str] = mapped_column(String(32), nullable=False)
    media_type: Mapped[str] = mapped_column(String(64), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    pdf_bytes: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)


class DecisionReportDeliveryRecord(Base):
    """An auditable attempt to deliver one exact report artifact."""

    __tablename__ = "decision_report_deliveries"
    scope_definition: ClassVar[ResourceScopeDefinition] = ResourceScopeDefinition(
        resource_name="econ.decision_report_delivery",
        table_name=__tablename__,
        operations=_ALL_OPERATIONS,
    )
    __table_args__ = (
        Index("ix_report_deliveries_tenant_created", "tenant_id", "created_at"),
    )

    delivery_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    report_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    report_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    recipients_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    delivery_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    last_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
