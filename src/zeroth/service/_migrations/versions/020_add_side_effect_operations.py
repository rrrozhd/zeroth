"""Durable side-effect operation records and lease generations.

Two coordination gaps close together here because they are the same failure in
different clothes: work being applied twice after ownership or liveness is lost.

``side_effect_operations`` gives every logical side-effecting operation one
durable row, so a repeat can be recognised instead of replayed blind.
``runs.lease_generation`` fences a worker that has lost its lease, so its writes
can be rejected rather than silently interleaved with the new owner's.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "020"
down_revision = "019"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(item["name"] == column for item in inspector.get_columns(table))


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS side_effect_operations (
            operation_key TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            dispatch_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            target_ref TEXT NOT NULL,
            attempt INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL,
            support TEXT NOT NULL DEFAULT 'at_least_once',
            receipt TEXT,
            error TEXT,
            ambiguity_reason TEXT,
            reconciliation_attempts INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    # Reconciliation work is claimed per run, and replay suppression looks the
    # operation up by key alone (already the primary key).
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_side_effect_operations_pending
            ON side_effect_operations(run_id, state)
    """)

    # A generation of 0 for existing rows is correct: every live lease predates
    # fencing, so the first post-upgrade claim advances past it.
    if not _has_column("runs", "lease_generation"):
        op.execute("ALTER TABLE runs ADD COLUMN lease_generation INTEGER NOT NULL DEFAULT 0")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_side_effect_operations_pending")
    op.execute("DROP TABLE IF EXISTS side_effect_operations")
    if _has_column("runs", "lease_generation"):
        op.execute("ALTER TABLE runs DROP COLUMN lease_generation")
