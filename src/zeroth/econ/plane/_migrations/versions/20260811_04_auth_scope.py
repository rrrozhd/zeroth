"""Bind authentication subjects to tenant and optional workspace scope.

Revision ID: 20260811_04
Revises: 20260712_03
Create Date: 2026-08-11
"""

from alembic import op
import sqlalchemy as sa

revision = "20260811_04"
down_revision = "20260712_03"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _tables()
    if "roles" not in tables:
        op.create_table(
            "roles",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("name", sa.String(length=64), nullable=False),
            sa.UniqueConstraint("name"),
        )
        op.create_index("ix_roles_name", "roles", ["name"], unique=True)

    if "users" not in tables:
        op.create_table(
            "users",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "tenant_id",
                sa.String(length=128),
                nullable=False,
                server_default="default",
            ),
            sa.Column("workspace_id", sa.String(length=128), nullable=True),
            sa.Column("subject", sa.String(length=128), nullable=False),
            sa.Column("email", sa.String(length=255), nullable=False),
            sa.UniqueConstraint("subject"),
            sa.UniqueConstraint("email"),
        )
        op.create_index("ix_users_tenant_id", "users", ["tenant_id"])
        op.create_index("ix_users_workspace_id", "users", ["workspace_id"])
        op.create_index("ix_users_subject", "users", ["subject"], unique=True)
        op.create_index("ix_users_email", "users", ["email"], unique=True)
    else:
        columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("users")}
        if "tenant_id" not in columns:
            op.add_column(
                "users",
                sa.Column(
                    "tenant_id",
                    sa.String(length=128),
                    nullable=True,
                    server_default="default",
                ),
            )
        if "workspace_id" not in columns:
            op.add_column("users", sa.Column("workspace_id", sa.String(length=128)))
        op.execute(
            sa.text(
                "UPDATE users SET tenant_id = 'default' "
                "WHERE tenant_id IS NULL OR tenant_id = 'tenant_default'"
            )
        )
        with op.batch_alter_table("users") as batch_op:
            batch_op.alter_column(
                "tenant_id",
                existing_type=sa.String(length=128),
                nullable=False,
                server_default="default",
            )
        indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("users")}
        if "ix_users_tenant_id" not in indexes:
            op.create_index("ix_users_tenant_id", "users", ["tenant_id"])
        if "ix_users_workspace_id" not in indexes:
            op.create_index("ix_users_workspace_id", "users", ["workspace_id"])

    tables = _tables()
    if "user_roles" not in tables:
        op.create_table(
            "user_roles",
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), primary_key=True),
            sa.Column("role_id", sa.Integer(), sa.ForeignKey("roles.id"), primary_key=True),
        )


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("users")}
    if "workspace_id" in columns:
        indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("users")}
        if "ix_users_workspace_id" in indexes:
            op.drop_index("ix_users_workspace_id", table_name="users")
        with op.batch_alter_table("users") as batch_op:
            batch_op.drop_column("workspace_id")
    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column(
            "tenant_id",
            existing_type=sa.String(length=128),
            nullable=True,
            server_default="tenant_default",
        )
