"""Add the persistent atomic external-cost reservation ledger.

Revision ID: 20260822_08
Revises: 20260812_07
Create Date: 2026-08-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260822_08"
down_revision = "20260812_07"
branch_labels = None
depends_on = None

_TABLE = "cost_reservations"
_FIELDS = (
    "tenant_id",
    "operation_id",
    "campaign_id",
    "run_id",
    "status",
    "max_cost_usd",
    "held_cost_usd",
    "actual_cost_usd",
    "released_cost_usd",
    "cost_measurement",
    "cost_event_id",
    "provider_request_id",
    "cleanup_status",
    "created_at",
    "updated_at",
)


def migration_plan(dialect_name: str) -> dict[str, object]:
    if dialect_name not in {"sqlite", "postgresql"}:
        raise ValueError(f"unsupported cost-reservation migration dialect: {dialect_name}")
    return {
        "table": _TABLE,
        "fields": _FIELDS,
        "unique_identity": ("tenant_id", "operation_id"),
        "tenant_scoped": True,
        "execution_event_fields": (
            "campaign_id",
            "operation_id",
            "provider_request_id",
            "cleanup_status",
        ),
    }


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "execution_events" in set(inspector.get_table_names()):
        present = {column["name"] for column in inspector.get_columns("execution_events")}
        for column in (
            sa.Column("campaign_id", sa.String(length=128), nullable=True),
            sa.Column("operation_id", sa.String(length=192), nullable=True),
            sa.Column("provider_request_id", sa.String(length=256), nullable=True),
            sa.Column("cleanup_status", sa.String(length=64), nullable=True),
        ):
            if column.name not in present:
                op.add_column("execution_events", column)
    if _TABLE in set(inspector.get_table_names()):
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("operation_id", sa.String(length=192), nullable=False),
        sa.Column("campaign_id", sa.String(length=128), nullable=True),
        sa.Column("run_id", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("max_cost_usd", sa.Numeric(18, 8), nullable=False),
        sa.Column("held_cost_usd", sa.Numeric(18, 8), nullable=False),
        sa.Column("actual_cost_usd", sa.Numeric(18, 8), nullable=True),
        sa.Column("released_cost_usd", sa.Numeric(18, 8), nullable=False),
        sa.Column("cost_measurement", sa.String(length=16), nullable=False),
        sa.Column("cost_event_id", sa.String(length=128), nullable=True),
        sa.Column("provider_request_id", sa.String(length=256), nullable=True),
        sa.Column("cleanup_status", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "operation_id", name="uq_cost_reservations_tenant_operation"
        ),
    )
    op.create_index(
        "ix_cost_reservations_tenant_id", _TABLE, ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_cost_reservations_tenant_status",
        _TABLE,
        ["tenant_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_cost_reservations_tenant_run", _TABLE, ["tenant_id", "run_id"], unique=False
    )
    op.create_index(
        "ix_cost_reservations_tenant_campaign",
        _TABLE,
        ["tenant_id", "campaign_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table(_TABLE)
    inspector = sa.inspect(op.get_bind())
    if "execution_events" in set(inspector.get_table_names()):
        present = {column["name"] for column in inspector.get_columns("execution_events")}
        for column in ("cleanup_status", "provider_request_id", "operation_id", "campaign_id"):
            if column in present:
                op.drop_column("execution_events", column)
