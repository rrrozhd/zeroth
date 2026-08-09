"""Persist checkpoint ownership independently of mutable thread lookups.

Revision ID: 022
Revises: 021
Create Date: 2026-08-09
"""

import sqlalchemy as sa
from alembic import op

revision = "022"
down_revision = "021"
branch_labels = None
depends_on = None

_LEGACY_COLUMNS = "checkpoint_id, run_id, thread_id, checkpoint_order, state_json, created_at"


def _create_scoped_checkpoints() -> None:
    op.execute("""
        CREATE TABLE run_checkpoints (
            checkpoint_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            checkpoint_order INTEGER NOT NULL,
            state_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            workspace_id TEXT,
            workspace_scope TEXT NOT NULL,
            CONSTRAINT run_checkpoints_scope_pkey
                PRIMARY KEY (tenant_id, workspace_scope, checkpoint_id)
        )
    """)


def upgrade() -> None:
    """Backfill only checkpoints with exactly one durable thread owner."""
    ambiguous = (
        op.get_bind()
        .execute(
            sa.text(
                """SELECT c.checkpoint_id
                   FROM run_checkpoints c
                   LEFT JOIN threads t ON t.thread_id = c.thread_id
                   GROUP BY c.checkpoint_id
                   HAVING COUNT(t.thread_id) <> 1
                   ORDER BY c.checkpoint_id LIMIT 1"""
            )
        )
        .scalar_one_or_none()
    )
    if ambiguous is not None:
        raise RuntimeError(
            f"checkpoint migration refused: ambiguous or missing thread owner {ambiguous!r}"
        )

    op.execute("ALTER TABLE run_checkpoints RENAME TO run_checkpoints_legacy_unscoped")
    _create_scoped_checkpoints()
    op.execute(f"""
        INSERT INTO run_checkpoints (
            {_LEGACY_COLUMNS}, tenant_id, workspace_id, workspace_scope
        )
        SELECT c.checkpoint_id, c.run_id, c.thread_id, c.checkpoint_order,
               c.state_json, c.created_at, t.tenant_id, t.workspace_id, t.workspace_scope
        FROM run_checkpoints_legacy_unscoped c
        JOIN threads t ON t.thread_id = c.thread_id
    """)
    op.execute("DROP TABLE run_checkpoints_legacy_unscoped")
    op.execute("""
        CREATE INDEX idx_run_checkpoints_owner_thread
            ON run_checkpoints(
                tenant_id, workspace_scope, thread_id, checkpoint_order, checkpoint_id
            )
    """)
    op.execute("""
        CREATE INDEX idx_run_checkpoints_owner_run
            ON run_checkpoints(tenant_id, workspace_scope, run_id, checkpoint_order)
    """)


def downgrade() -> None:
    """Restore the global checkpoint ID only when scoped IDs do not collide."""
    duplicate = (
        op.get_bind()
        .execute(
            sa.text(
                """SELECT checkpoint_id FROM run_checkpoints
                   GROUP BY checkpoint_id HAVING COUNT(*) > 1
                   ORDER BY checkpoint_id LIMIT 1"""
            )
        )
        .scalar_one_or_none()
    )
    if duplicate is not None:
        raise RuntimeError(
            f"checkpoint downgrade refused: duplicate global checkpoint_id {duplicate!r}"
        )

    op.execute("ALTER TABLE run_checkpoints RENAME TO run_checkpoints_scoped_owner")
    op.execute("""
        CREATE TABLE run_checkpoints (
            checkpoint_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            checkpoint_order INTEGER NOT NULL,
            state_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    op.execute(f"""
        INSERT INTO run_checkpoints ({_LEGACY_COLUMNS})
        SELECT {_LEGACY_COLUMNS} FROM run_checkpoints_scoped_owner
    """)
    op.execute("DROP TABLE run_checkpoints_scoped_owner")
