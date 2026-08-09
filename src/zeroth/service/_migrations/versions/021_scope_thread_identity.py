"""Scope logical thread identity by tenant and workspace.

Revision ID: 021
Revises: 020
Create Date: 2026-08-09
"""

import sqlalchemy as sa
from alembic import op

revision = "021"
down_revision = "020"
branch_labels = None
depends_on = None

_COLUMNS = """
    thread_id, graph_version_ref, deployment_ref, status,
    participating_agent_refs, state_snapshot_refs, checkpoint_refs,
    memory_bindings, run_ids, active_run_id, last_run_id,
    created_at, updated_at, tenant_id, workspace_id
"""


def _create_scoped_threads() -> None:
    op.execute("""
        CREATE TABLE threads (
            thread_id TEXT NOT NULL,
            graph_version_ref TEXT NOT NULL,
            deployment_ref TEXT NOT NULL,
            status TEXT NOT NULL,
            participating_agent_refs TEXT NOT NULL,
            state_snapshot_refs TEXT NOT NULL,
            checkpoint_refs TEXT NOT NULL,
            memory_bindings TEXT NOT NULL,
            run_ids TEXT NOT NULL,
            active_run_id TEXT,
            last_run_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            workspace_id TEXT,
            workspace_scope TEXT NOT NULL,
            CONSTRAINT threads_scope_pkey PRIMARY KEY (tenant_id, workspace_scope, thread_id)
        )
    """)


def upgrade() -> None:
    """Rebuild threads with scope-local logical IDs and preserve every row."""
    blank_tenants = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT COUNT(*) FROM threads WHERE tenant_id IS NOT NULL AND TRIM(tenant_id) = ''"
            )
        )
        .scalar_one()
    )
    if blank_tenants:
        raise RuntimeError("threads migration refused: blank tenant_id")
    op.execute("ALTER TABLE threads RENAME TO threads_legacy_global_id")
    _create_scoped_threads()
    op.execute(f"""
        INSERT INTO threads ({_COLUMNS}, workspace_scope)
        SELECT
               thread_id, graph_version_ref, deployment_ref, status,
               participating_agent_refs, state_snapshot_refs, checkpoint_refs,
               memory_bindings, run_ids, active_run_id, last_run_id,
               created_at, updated_at, COALESCE(tenant_id, 'default'), workspace_id,
               CASE
                   WHEN workspace_id IS NULL THEN 'null'
                   ELSE 'value:' || workspace_id
               END
        FROM threads_legacy_global_id
    """)
    op.execute("DROP TABLE threads_legacy_global_id")
    op.execute("""
        CREATE INDEX idx_threads_scope
            ON threads(tenant_id, workspace_id, deployment_ref, thread_id)
    """)


def downgrade() -> None:
    """Restore global logical IDs; refuses naturally if scoped duplicates exist."""
    duplicate = (
        op.get_bind()
        .execute(
            sa.text(
                """SELECT thread_id FROM threads
               GROUP BY thread_id HAVING COUNT(*) > 1
               ORDER BY thread_id LIMIT 1"""
            )
        )
        .scalar_one_or_none()
    )
    if duplicate is not None:
        raise RuntimeError(f"threads downgrade refused: duplicate global thread_id {duplicate!r}")
    op.execute("ALTER TABLE threads RENAME TO threads_scoped_identity")
    op.execute("""
        CREATE TABLE threads (
            thread_id TEXT PRIMARY KEY,
            graph_version_ref TEXT NOT NULL,
            deployment_ref TEXT NOT NULL,
            status TEXT NOT NULL,
            participating_agent_refs TEXT NOT NULL,
            state_snapshot_refs TEXT NOT NULL,
            checkpoint_refs TEXT NOT NULL,
            memory_bindings TEXT NOT NULL,
            run_ids TEXT NOT NULL,
            active_run_id TEXT,
            last_run_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            tenant_id TEXT DEFAULT 'default',
            workspace_id TEXT
        )
    """)
    op.execute(f"INSERT INTO threads ({_COLUMNS}) SELECT {_COLUMNS} FROM threads_scoped_identity")
    op.execute("DROP TABLE threads_scoped_identity")
    op.execute("""
        CREATE INDEX idx_threads_scope
            ON threads(tenant_id, workspace_id, deployment_ref, thread_id)
    """)
