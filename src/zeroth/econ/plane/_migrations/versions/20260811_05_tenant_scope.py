"""Make econ operational ownership and execution identity tenant structural.

Revision ID: 20260811_05
Revises: 20260811_04
Create Date: 2026-08-11
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260811_05"
down_revision = "20260811_04"
branch_labels = None
depends_on = None

_SCOPE_TABLES = (
    "execution_events",
    "outcome_events",
    "valuation_runs",
    "value_estimates",
    "connector_configs",
    "connector_outbox",
    "connector_delivery_log",
)
_EXECUTION_UNIQUE = ("tenant_id", "execution_id")
_EXECUTION_UNIQUE_NAME = "uq_execution_events_tenant_execution_id"


def migration_plan(dialect_name: str) -> dict[str, object]:
    """Return the invariant plan shared by SQLite and PostgreSQL execution."""
    if dialect_name not in {"sqlite", "postgresql"}:
        raise ValueError(f"unsupported tenant-scope migration dialect: {dialect_name}")
    return {
        "scope_tables": _SCOPE_TABLES,
        "execution_unique": _EXECUTION_UNIQUE,
        "normalizes_reserved_default": True,
        "removes_server_defaults": True,
    }


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _table_names() -> set[str]:
    return set(_inspector().get_table_names())


def _columns(table_name: str) -> dict[str, dict[str, object]]:
    return {column["name"]: column for column in _inspector().get_columns(table_name)}


def _tenant_index_name(table_name: str) -> str:
    return f"ix_{table_name}_tenant_id"


def _ensure_tenant_column(table_name: str) -> None:
    if "tenant_id" in _columns(table_name):
        return
    op.add_column(
        table_name,
        sa.Column("tenant_id", sa.String(length=128), nullable=True),
    )


def _backfill_delivery_ownership(tables: set[str]) -> None:
    if "connector_outbox" in tables:
        op.execute(
            sa.text(
                "UPDATE connector_delivery_log "
                "SET tenant_id = COALESCE(("
                "SELECT connector_outbox.tenant_id FROM connector_outbox "
                "WHERE connector_outbox.id = connector_delivery_log.outbox_id"
                "), 'default') "
                "WHERE tenant_id IS NULL OR tenant_id = 'tenant_default'"
            )
        )
        return
    op.execute(
        sa.text(
            "UPDATE connector_delivery_log SET tenant_id = 'default' "
            "WHERE tenant_id IS NULL OR tenant_id = 'tenant_default'"
        )
    )


def _normalize_tenant(table_name: str, tables: set[str]) -> None:
    needs_normalization = op.get_bind().execute(
        sa.text(
            f"SELECT 1 FROM {table_name} "
            "WHERE tenant_id IS NULL OR tenant_id = 'tenant_default' LIMIT 1"
        )
    ).first()
    if needs_normalization is None:
        return
    if table_name == "connector_delivery_log":
        _backfill_delivery_ownership(tables)
        return
    op.execute(
        sa.text(
            f"UPDATE {table_name} SET tenant_id = 'default' "
            "WHERE tenant_id IS NULL OR tenant_id = 'tenant_default'"
        )
    )


def _execution_unique_constraints() -> list[dict[str, object]]:
    return list(_inspector().get_unique_constraints("execution_events"))


def _make_ownership_strict(table_name: str) -> None:
    tenant_column = _columns(table_name)["tenant_id"]
    if tenant_column["nullable"] is False and tenant_column["default"] is None:
        return
    with op.batch_alter_table(table_name) as batch_op:
        batch_op.alter_column(
            "tenant_id",
            existing_type=tenant_column["type"],
            nullable=False,
            server_default=None,
        )


def _replace_execution_unique() -> None:
    constraints = _execution_unique_constraints()
    old_names = [
        constraint["name"]
        for constraint in constraints
        if tuple(constraint["column_names"]) == ("execution_id",)
        and constraint["name"] is not None
    ]
    has_composite = any(
        tuple(constraint["column_names"]) == _EXECUTION_UNIQUE
        for constraint in constraints
    )
    if not old_names and has_composite:
        return
    with op.batch_alter_table("execution_events") as batch_op:
        for name in old_names:
            batch_op.drop_constraint(name, type_="unique")
        if not has_composite:
            batch_op.create_unique_constraint(
                _EXECUTION_UNIQUE_NAME,
                list(_EXECUTION_UNIQUE),
            )


def _ensure_tenant_index(table_name: str) -> None:
    inspector = _inspector()
    indexes = inspector.get_indexes(table_name)
    if any(tuple(index["column_names"]) == ("tenant_id",) for index in indexes):
        return
    op.create_index(
        _tenant_index_name(table_name),
        table_name,
        ["tenant_id"],
        unique=False,
    )


def upgrade() -> None:
    tables = _table_names()
    migration_plan(op.get_bind().dialect.name)
    for table_name in _SCOPE_TABLES:
        if table_name not in tables:
            continue
        _ensure_tenant_column(table_name)
        _normalize_tenant(table_name, tables)
        _make_ownership_strict(table_name)
        _ensure_tenant_index(table_name)
    if "execution_events" in tables:
        _replace_execution_unique()


def downgrade() -> None:
    tables = _table_names()
    if "execution_events" in tables:
        constraints = _execution_unique_constraints()
        with op.batch_alter_table("execution_events") as batch_op:
            for constraint in constraints:
                if (
                    tuple(constraint["column_names"]) == _EXECUTION_UNIQUE
                    and constraint["name"] is not None
                ):
                    batch_op.drop_constraint(constraint["name"], type_="unique")
            batch_op.create_unique_constraint(
                "uq_execution_events_execution_id",
                ["execution_id"],
            )
    for table_name in reversed(_SCOPE_TABLES):
        if table_name not in tables:
            continue
        if table_name == "connector_delivery_log":
            index_names = {
                index["name"] for index in _inspector().get_indexes(table_name)
            }
            index_name = _tenant_index_name(table_name)
            if index_name in index_names:
                op.drop_index(index_name, table_name=table_name)
            with op.batch_alter_table(table_name) as batch_op:
                batch_op.drop_column("tenant_id")
            continue
        column = _columns(table_name)["tenant_id"]
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.alter_column(
                "tenant_id",
                existing_type=column["type"],
                nullable=False,
                server_default="tenant_default",
            )
