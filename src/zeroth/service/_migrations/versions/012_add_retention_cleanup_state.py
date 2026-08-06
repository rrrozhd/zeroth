"""Materialize current retention cleanup state for constant-time fencing."""

from alembic import op

revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE retention_cleanup_state (
            authorization_log_id TEXT PRIMARY KEY
                REFERENCES retention_audit_log(log_id) ON DELETE CASCADE,
            tenant_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            reason TEXT NOT NULL CHECK (reason IN ('ttl', 'rte', 'manual')),
            generation INTEGER NOT NULL DEFAULT 0 CHECK (generation >= 0),
            revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
            active_claim_id TEXT,
            active_claim_log_id TEXT,
            lease_expires_at TEXT,
            terminal_status TEXT CHECK (terminal_status IN ('completed', 'failed')),
            terminal_log_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (
                (active_claim_id IS NULL AND active_claim_log_id IS NULL
                    AND lease_expires_at IS NULL)
                OR
                (active_claim_id IS NOT NULL AND active_claim_log_id IS NOT NULL
                    AND lease_expires_at IS NOT NULL)
            ),
            CHECK (
                (terminal_status IS NULL AND terminal_log_id IS NULL)
                OR
                (terminal_status IS NOT NULL AND terminal_log_id IS NOT NULL)
            )
        )
    """)
    op.execute("""
        CREATE INDEX idx_retention_cleanup_state_tenant_run
            ON retention_cleanup_state(tenant_id, run_id)
    """)
    op.execute("""
        CREATE TABLE retention_cleanup_operations (
            authorization_log_id TEXT NOT NULL
                REFERENCES retention_cleanup_state(authorization_log_id) ON DELETE CASCADE,
            operation_id TEXT NOT NULL,
            status TEXT NOT NULL
                CHECK (status IN ('pending', 'in_progress', 'completed', 'failed', 'skipped')),
            deleted_count INTEGER,
            error TEXT,
            revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
            updated_at TEXT NOT NULL,
            PRIMARY KEY (authorization_log_id, operation_id)
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE retention_cleanup_operations")
    op.execute("DROP INDEX idx_retention_cleanup_state_tenant_run")
    op.execute("DROP TABLE retention_cleanup_state")
