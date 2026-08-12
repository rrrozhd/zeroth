from typing import ClassVar

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from zeroth.econ.plane.database import Base
from zeroth.platform.storage.scoping import ResourceOperation, ResourceScope, ResourceScopeDefinition

_ALL_OPERATIONS = frozenset(ResourceOperation)


class UserRole(Base):
    __tablename__ = "user_roles"
    scope_definition: ClassVar[ResourceScopeDefinition] = ResourceScopeDefinition(
        resource_name="econ.user_role",
        table_name=__tablename__,
        operations=_ALL_OPERATIONS,
        scope=ResourceScope.GLOBAL,
    )

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"), primary_key=True)


class User(Base):
    __tablename__ = "users"
    scope_definition: ClassVar[ResourceScopeDefinition] = ResourceScopeDefinition(
        resource_name="econ.user", table_name=__tablename__, operations=_ALL_OPERATIONS
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, default="default")
    workspace_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    subject: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)

    roles: Mapped[list["Role"]] = relationship(secondary="user_roles", back_populates="users")


class Role(Base):
    __tablename__ = "roles"
    scope_definition: ClassVar[ResourceScopeDefinition] = ResourceScopeDefinition(
        resource_name="econ.role",
        table_name=__tablename__,
        operations=_ALL_OPERATIONS,
        scope=ResourceScope.GLOBAL,
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    users: Mapped[list[User]] = relationship(secondary="user_roles", back_populates="roles")
