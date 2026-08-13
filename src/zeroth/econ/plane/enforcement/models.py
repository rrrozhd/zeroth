from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column

from zeroth.econ.plane.database import Base
from zeroth.platform.storage.scoping import ResourceOperation, ResourceScopeDefinition

_ALL_OPERATIONS = frozenset(ResourceOperation)


class EnforcementAction(Base):
    __tablename__ = "enforcement_actions"
    scope_definition: ClassVar[ResourceScopeDefinition] = ResourceScopeDefinition(
        resource_name="econ.enforcement_action", table_name=__tablename__, operations=_ALL_OPERATIONS  # noqa: E501
    )
    __table_args__ = (Index("ix_enforcement_actions_status_created", "status", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, default="tenant_default")
    capability_id: Mapped[str] = mapped_column(String(128), index=True)
    action_type: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    reason: Mapped[str] = mapped_column(String(512))
    before_config: Mapped[dict] = mapped_column(JSON, default=dict)
    after_config: Mapped[dict] = mapped_column(JSON, default=dict)
    approver_sub: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class PolicyAction(Base):
    __tablename__ = "policy_actions"
    scope_definition: ClassVar[ResourceScopeDefinition] = ResourceScopeDefinition(
        resource_name="econ.policy_action", table_name=__tablename__, operations=_ALL_OPERATIONS
    )
    __table_args__ = (Index("ix_policy_actions_tenant_status_proposed", "tenant_id", "status", "proposed_at"),)  # noqa: E501

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, default="tenant_default")
    capability_id: Mapped[str] = mapped_column(String(128), index=True)
    #: The enforcement action this row was proposed for, when there was one.
    #: NULL means "unlinked": either the row predates the column, or it was
    #: proposed outside an enforcement decision.  An unlinked row is never
    #: resolved back to an enforcement action by recency -- see A01-11.
    enforcement_action_id: Mapped[int | None] = mapped_column(
        ForeignKey("enforcement_actions.id"), nullable=True
    )
    proposed_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    proposed_by: Mapped[str] = mapped_column(String(128), default="system")
    action_type: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    metrics_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("performance_snapshots.id"), nullable=True)  # noqa: E501
    confidence_state_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="PROPOSED")
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)


class TrafficPolicy(Base):
    __tablename__ = "traffic_policies"
    scope_definition: ClassVar[ResourceScopeDefinition] = ResourceScopeDefinition(
        resource_name="econ.traffic_policy", table_name=__tablename__, operations=_ALL_OPERATIONS
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, default="tenant_default")
    capability_id: Mapped[str] = mapped_column(String(128), index=True)
    weights: Mapped[dict] = mapped_column(JSON, default=dict)


class BudgetPolicy(Base):
    __tablename__ = "budget_policies"
    scope_definition: ClassVar[ResourceScopeDefinition] = ResourceScopeDefinition(
        resource_name="econ.budget_policy", table_name=__tablename__, operations=_ALL_OPERATIONS
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, default="tenant_default")
    capability_id: Mapped[str] = mapped_column(String(128), index=True)
    monthly_cap_usd: Mapped[float] = mapped_column()


class AuditLog(Base):
    __tablename__ = "audit_log"
    scope_definition: ClassVar[ResourceScopeDefinition] = ResourceScopeDefinition(
        resource_name="econ.audit_log", table_name=__tablename__, operations=_ALL_OPERATIONS
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, default="tenant_default")
    actor_sub: Mapped[str] = mapped_column(String(128), index=True)
    action: Mapped[str] = mapped_column(String(128))
    entity_type: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class TenantBudget(Base):
    __tablename__ = "tenant_budgets"
    scope_definition: ClassVar[ResourceScopeDefinition] = ResourceScopeDefinition(
        resource_name="econ.tenant_budget", table_name=__tablename__, operations=_ALL_OPERATIONS
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    budget_cap_usd: Mapped[float] = mapped_column()
    updated_at: Mapped[datetime] = mapped_column(DateTime)
