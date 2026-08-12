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

_PROVENANCE_TABLE = "_zeroth_20260811_04_auth_scope"


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _record_provenance(
    *,
    created_roles: bool,
    created_users: bool,
    created_user_roles: bool,
    added_tenant: bool,
    added_workspace: bool,
    added_tenant_index: bool,
    added_workspace_index: bool,
    prior_tenant_nullable: bool | None,
    prior_tenant_default: str | None,
) -> None:
    op.create_table(
        _PROVENANCE_TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_roles", sa.Boolean(), nullable=False),
        sa.Column("created_users", sa.Boolean(), nullable=False),
        sa.Column("created_user_roles", sa.Boolean(), nullable=False),
        sa.Column("added_tenant", sa.Boolean(), nullable=False),
        sa.Column("added_workspace", sa.Boolean(), nullable=False),
        sa.Column("added_tenant_index", sa.Boolean(), nullable=False),
        sa.Column("added_workspace_index", sa.Boolean(), nullable=False),
        sa.Column("prior_tenant_nullable", sa.Boolean(), nullable=True),
        sa.Column("prior_tenant_default", sa.Text(), nullable=True),
    )
    table = sa.table(
        _PROVENANCE_TABLE,
        sa.column("id"),
        sa.column("created_roles"),
        sa.column("created_users"),
        sa.column("created_user_roles"),
        sa.column("added_tenant"),
        sa.column("added_workspace"),
        sa.column("added_tenant_index"),
        sa.column("added_workspace_index"),
        sa.column("prior_tenant_nullable"),
        sa.column("prior_tenant_default"),
    )
    op.execute(
        table.insert().values(
            id=1,
            created_roles=created_roles,
            created_users=created_users,
            created_user_roles=created_user_roles,
            added_tenant=added_tenant,
            added_workspace=added_workspace,
            added_tenant_index=added_tenant_index,
            added_workspace_index=added_workspace_index,
            prior_tenant_nullable=prior_tenant_nullable,
            prior_tenant_default=prior_tenant_default,
        )
    )


def upgrade() -> None:
    tables = _tables()
    created_roles = "roles" not in tables
    created_users = "users" not in tables
    created_user_roles = "user_roles" not in tables
    columns = (
        {column["name"]: column for column in sa.inspect(op.get_bind()).get_columns("users")}
        if not created_users
        else {}
    )
    indexes = (
        {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("users")}
        if not created_users
        else set()
    )
    added_tenant = not created_users and "tenant_id" not in columns
    added_workspace = not created_users and "workspace_id" not in columns
    added_tenant_index = not created_users and "ix_users_tenant_id" not in indexes
    added_workspace_index = not created_users and "ix_users_workspace_id" not in indexes
    tenant_column = columns.get("tenant_id")
    _record_provenance(
        created_roles=created_roles,
        created_users=created_users,
        created_user_roles=created_user_roles,
        added_tenant=added_tenant,
        added_workspace=added_workspace,
        added_tenant_index=added_tenant_index,
        added_workspace_index=added_workspace_index,
        prior_tenant_nullable=tenant_column["nullable"] if tenant_column else None,
        prior_tenant_default=tenant_column["default"] if tenant_column else None,
    )
    if created_roles:
        op.create_table(
            "roles",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("name", sa.String(length=64), nullable=False),
            sa.UniqueConstraint("name"),
        )
        op.create_index("ix_roles_name", "roles", ["name"], unique=True)

    if created_users:
        op.create_table(
            "users",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "tenant_id",
                sa.String(length=128),
                nullable=False,
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
        if added_tenant:
            op.add_column(
                "users",
                sa.Column(
                    "tenant_id",
                    sa.String(length=128),
                    nullable=True,
                    server_default="default",
                ),
            )
        if added_workspace:
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
                server_default=None,
            )
        if added_tenant_index:
            op.create_index("ix_users_tenant_id", "users", ["tenant_id"])
        if added_workspace_index:
            op.create_index("ix_users_workspace_id", "users", ["workspace_id"])

    if created_user_roles:
        op.create_table(
            "user_roles",
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), primary_key=True),
            sa.Column("role_id", sa.Integer(), sa.ForeignKey("roles.id"), primary_key=True),
        )


def downgrade() -> None:
    provenance = (
        op.get_bind()
        .execute(sa.text(f"SELECT * FROM {_PROVENANCE_TABLE} WHERE id = 1"))
        .mappings()
        .one()
    )

    if provenance["created_user_roles"]:
        op.drop_table("user_roles")

    if provenance["created_users"]:
        op.drop_table("users")
    else:
        if provenance["added_workspace_index"]:
            op.drop_index("ix_users_workspace_id", table_name="users")
        if provenance["added_workspace"]:
            with op.batch_alter_table("users") as batch_op:
                batch_op.drop_column("workspace_id")

        if provenance["added_tenant_index"]:
            op.drop_index("ix_users_tenant_id", table_name="users")
        if provenance["added_tenant"]:
            with op.batch_alter_table("users") as batch_op:
                batch_op.drop_column("tenant_id")
        else:
            prior_default = provenance["prior_tenant_default"]
            with op.batch_alter_table("users") as batch_op:
                batch_op.alter_column(
                    "tenant_id",
                    existing_type=sa.String(length=128),
                    nullable=bool(provenance["prior_tenant_nullable"]),
                    server_default=sa.text(prior_default) if prior_default is not None else None,
                )

    if provenance["created_roles"]:
        op.drop_table("roles")
    op.drop_table(_PROVENANCE_TABLE)
