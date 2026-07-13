from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, String
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from zeroth.econ_plane.database import Base


class Capability(Base):
    __tablename__ = "capabilities"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, default="tenant_default")
    name: Mapped[str] = mapped_column(String(255))
    legacy_category: Mapped[str] = mapped_column("category", String(64), index=True, default="RiskMitigation")
    capability_type: Mapped[str] = mapped_column("type", String(64), index=True, default="RISK")
    description: Mapped[str] = mapped_column(String(1024), default="")
    criticality: Mapped[str] = mapped_column(String(16), default="MED")
    is_protected: Mapped[bool] = mapped_column(Boolean, default=False)
    valuation_config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    implementations: Mapped[list["Implementation"]] = relationship(back_populates="capability")


class Implementation(Base):
    __tablename__ = "implementations"
    __table_args__ = (Index("ix_implementation_capability_period", "capability_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, default="tenant_default")
    capability_id: Mapped[str] = mapped_column(ForeignKey("capabilities.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    provider: Mapped[str] = mapped_column(String(32), default="custom")
    model_name: Mapped[str] = mapped_column(String(255), default="")
    model_version_hash: Mapped[str] = mapped_column(String(128), default="")
    prompt_version_hash: Mapped[str] = mapped_column(String(128), default="")
    pipeline_version_hash: Mapped[str] = mapped_column(String(128), default="")
    config_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE")
    model_version: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    capability: Mapped[Capability] = relationship(back_populates="implementations")


class Deployment(Base):
    __tablename__ = "deployments"
    __table_args__ = (Index("ix_deployments_tenant_capability", "tenant_id", "capability_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, default="tenant_default")
    capability_id: Mapped[str] = mapped_column(ForeignKey("capabilities.id"), index=True)
    weights_json: Mapped[dict] = mapped_column(JSON, default=dict)
    routing_mode: Mapped[str] = mapped_column(String(32), default="STATIC")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class DeploymentImplementation(Base):
    __tablename__ = "deployment_implementations"

    deployment_id: Mapped[int] = mapped_column(ForeignKey("deployments.id"), primary_key=True)
    implementation_id: Mapped[str] = mapped_column(ForeignKey("implementations.id"), primary_key=True)


class Experiment(Base):
    __tablename__ = "experiments"
    __table_args__ = (Index("ix_experiments_tenant_capability_status", "tenant_id", "capability_id", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, default="tenant_default")
    capability_id: Mapped[str] = mapped_column(ForeignKey("capabilities.id"), index=True)
    mode: Mapped[str] = mapped_column(String(16), default="AB")
    impl_a_id: Mapped[str] = mapped_column(String(128), index=True)
    impl_b_id: Mapped[str] = mapped_column(String(128), index=True)
    start_at: Mapped[datetime] = mapped_column(DateTime)
    end_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    target_pct: Mapped[float] = mapped_column(Float, default=50.0)
    assignment_key: Mapped[str] = mapped_column(String(32), default="request_id")
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE")
    constraints_json: Mapped[dict] = mapped_column(JSON, default=dict)
