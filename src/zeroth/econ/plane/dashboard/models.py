from typing import ClassVar

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from zeroth.econ.plane.database import Base
from zeroth.platform.storage.scoping import ResourceOperation, ResourceScopeDefinition

_ALL_OPERATIONS = frozenset(ResourceOperation)


class DashboardView(Base):
    __tablename__ = "dashboard_views"
    scope_definition: ClassVar[ResourceScopeDefinition] = ResourceScopeDefinition(
        resource_name="econ.dashboard_view", table_name=__tablename__, operations=_ALL_OPERATIONS
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, default="tenant_default")
    viewer_sub: Mapped[str] = mapped_column(String(128), index=True)
    view_name: Mapped[str] = mapped_column(String(64))
