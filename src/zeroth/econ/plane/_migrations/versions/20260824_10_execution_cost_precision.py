"""Retain sub-cent execution-event cost precision.

Revision ID: 20260824_10
Revises: 20260823_09
Create Date: 2026-08-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260824_10"
down_revision = "20260823_09"
branch_labels = None
depends_on = None

_COST_COLUMNS = ("token_cost_usd", "tool_cost_usd", "compute_cost_usd")
_OLD_TYPE = sa.Numeric(12, 4)
_NEW_TYPE = sa.Numeric(18, 8)


def _alter_cost_columns(*, target_type: sa.Numeric, existing_type: sa.Numeric) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "execution_events" not in inspector.get_table_names():
        return
    present_columns = {column["name"] for column in inspector.get_columns("execution_events")}
    columns = [column for column in _COST_COLUMNS if column in present_columns]
    if not columns:
        return

    # SQLite cannot ALTER a column type in place, so Alembic must rebuild and
    # copy the table. PostgreSQL uses a normal ALTER COLUMN TYPE, avoiding an
    # unnecessary table recreation and preserving the database's constraints.
    recreate = "always" if bind.dialect.name == "sqlite" else "auto"
    with op.batch_alter_table("execution_events", recreate=recreate) as batch:
        for column in columns:
            batch.alter_column(
                column,
                existing_type=existing_type,
                type_=target_type,
                existing_nullable=True,
            )


def upgrade() -> None:
    _alter_cost_columns(target_type=_NEW_TYPE, existing_type=_OLD_TYPE)


def downgrade() -> None:
    _alter_cost_columns(target_type=_OLD_TYPE, existing_type=_NEW_TYPE)
