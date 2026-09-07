"""Retain first execution ingestion time without inventing historical arrivals."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260907_24"
down_revision = "20260906_23"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "execution_events" not in inspector.get_table_names():
        return
    if "ingested_at" not in {col["name"] for col in inspector.get_columns("execution_events")}:
        op.add_column("execution_events", sa.Column("ingested_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "execution_events" not in inspector.get_table_names():
        return
    if "ingested_at" not in {col["name"] for col in inspector.get_columns("execution_events")}:
        return
    if op.get_bind().execute(sa.text(
        "SELECT 1 FROM execution_events WHERE ingested_at IS NOT NULL LIMIT 1"
    )).first() is not None:
        raise RuntimeError("Cannot discard execution arrival times; use a compatible reader rollback")
    op.drop_column("execution_events", "ingested_at")
