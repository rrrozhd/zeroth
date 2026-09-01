from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from sqlalchemy import DateTime, String, UniqueConstraint
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column

from zeroth.econ.plane.database import Base
from zeroth.platform.storage.scoping import ResourceOperation, ResourceScopeDefinition

_ALL_OPERATIONS = frozenset(ResourceOperation)


class OutcomeDefinition(Base):
    """Immutable success semantics for one workflow version."""

    __tablename__ = "outcome_definitions"
    scope_definition: ClassVar[ResourceScopeDefinition] = ResourceScopeDefinition(
        resource_name="econ.outcome_definition",
        table_name=__tablename__,
        operations=_ALL_OPERATIONS,
    )
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "workflow_id",
            "workflow_version",
            name="uq_outcome_definitions_tenant_workflow_version",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    workflow_id: Mapped[str] = mapped_column(String(192), index=True, nullable=False)
    workflow_version: Mapped[str] = mapped_column(String(192), nullable=False)
    outcome_type: Mapped[str] = mapped_column(String(64), nullable=False)
    operator: Mapped[str] = mapped_column(String(32), nullable=False)
    target_json: Mapped[object] = mapped_column(JSON, nullable=False)
    definition_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
