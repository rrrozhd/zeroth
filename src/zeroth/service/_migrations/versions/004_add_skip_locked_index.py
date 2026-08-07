"""Add partial index for multi-worker SKIP LOCKED dispatch.

Revision ID: 004
Revises: 003
Create Date: 2026-04-07

Creates a partial index on runs(deployment_ref, status, started_at) filtered
to status='pending' rows.  This index accelerates the SKIP LOCKED claiming
query used by the Postgres backend of LeaseManager.  SQLite ignores the
WHERE clause but the index columns still help with the existing SQLite path.
"""

from alembic import op

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Partial index optimizes the SKIP LOCKED claiming query for Postgres.
    # SQLite ignores the WHERE clause in indexes but the index still works.
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_runs_pending_claim
            ON runs(deployment_ref, status, started_at)
            WHERE status = 'pending'
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_runs_pending_claim")
