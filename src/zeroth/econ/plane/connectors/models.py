from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from sqlalchemy import DateTime, Index, String, UniqueConstraint
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column

from zeroth.econ.plane.database import Base
from zeroth.platform.storage.scoping import ResourceOperation, ResourceScopeDefinition

_ALL_OPERATIONS = frozenset(ResourceOperation)


class ConnectorConfig(Base):
    __tablename__ = "connector_configs"
    scope_definition: ClassVar[ResourceScopeDefinition] = ResourceScopeDefinition(
        resource_name="econ.connector_config", table_name=__tablename__, operations=_ALL_OPERATIONS
    )
    __table_args__ = (Index("ix_connector_configs_tenant_type_enabled", "tenant_id", "connector_type", "enabled"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    connector_type: Mapped[str] = mapped_column(String(64), index=True)
    enabled: Mapped[bool] = mapped_column(default=False)
    config_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class ConnectorOutbox(Base):
    __tablename__ = "connector_outbox"
    scope_definition: ClassVar[ResourceScopeDefinition] = ResourceScopeDefinition(
        resource_name="econ.connector_outbox", table_name=__tablename__, operations=_ALL_OPERATIONS
    )
    __table_args__ = (
        UniqueConstraint("tenant_id", "event_type", "event_key", name="uq_connector_outbox_tenant_event_key"),
        Index("ix_connector_outbox_status_next_attempt", "status", "next_attempt_at"),
        Index("ix_connector_outbox_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), index=True)
    event_key: Mapped[str] = mapped_column(String(255), index=True)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    last_error: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ConnectorDeliveryLog(Base):
    __tablename__ = "connector_delivery_log"
    scope_definition: ClassVar[ResourceScopeDefinition] = ResourceScopeDefinition(
        resource_name="econ.connector_delivery_log",
        table_name=__tablename__,
        operations=_ALL_OPERATIONS,
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    outbox_id: Mapped[int] = mapped_column(index=True)
    connector_type: Mapped[str] = mapped_column(String(64), index=True)
    attempt: Mapped[int] = mapped_column(default=1)
    status_code: Mapped[int | None] = mapped_column(nullable=True)
    response_excerpt: Mapped[str | None] = mapped_column(String(512), nullable=True)
    duration_ms: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)
