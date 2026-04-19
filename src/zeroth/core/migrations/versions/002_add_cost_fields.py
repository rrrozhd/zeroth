"""Add cost attribution fields to node_audits.

Revision ID: 002
Revises: 001
Create Date: 2026-04-07

Adds cost_usd (REAL) and cost_event_id (TEXT) columns to the node_audits
table to support per-call cost attribution from the Regulus economics module.
"""

import sqlalchemy as sa
from alembic import op

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add cost_usd and cost_event_id columns to node_audits."""
    op.add_column("node_audits", sa.Column("cost_usd", sa.Float(), nullable=True))
    op.add_column("node_audits", sa.Column("cost_event_id", sa.Text(), nullable=True))


def downgrade() -> None:
    """Remove cost_usd and cost_event_id columns from node_audits."""
    op.drop_column("node_audits", "cost_event_id")
    op.drop_column("node_audits", "cost_usd")
