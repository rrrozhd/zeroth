"""Preserve explicit outcome maturity without reclassifying legacy observations."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260906_21"
down_revision = "20260906_20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "outcome_events" not in inspector.get_table_names():
        return
    if "maturity" not in {column["name"] for column in inspector.get_columns("outcome_events")}:
        op.add_column("outcome_events", sa.Column("maturity", sa.String(16), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "outcome_events" not in inspector.get_table_names():
        return
    if "maturity" not in {column["name"] for column in inspector.get_columns("outcome_events")}:
        return
    if op.get_bind().execute(sa.text(
        "SELECT 1 FROM outcome_events WHERE maturity IS NOT NULL AND maturity != 'unknown' LIMIT 1"
    )).first() is not None:
        raise RuntimeError("Cannot discard outcome maturity; use a compatible reader rollback")
    op.drop_column("outcome_events", "maturity")
