"""Add WS-E retention / right-to-erasure tables and the node_audits purge index.

Revision ID: 008
Revises: 007
Create Date: 2026-07-11

GDPR data-minimization: per-tenant retention TTLs, legal holds that beat both
TTL purge and explicit erasure, and an append-only retention_audit_log recording
every erasure step.

Tables only — ``node_audits`` gets NO column ADD: the WS-E erasure fields
(``erased``, ``erased_at``, ``erasure_reason``, ``digest_version``,
``pii_commitments``) live inside the existing ``record_json`` TEXT blob, so no
DDL and no re-serialization of any existing row is required. Pre-008 rows are
therefore grandfathered as digest_version=1 automatically (the model default
fills in on load) and are NEVER re-chained — re-chaining history would itself be
a tamper event.

Raw ``op.execute`` keeps the DDL portable across SQLite and Postgres, matching
the 001-007 convention. Booleans are stored as INTEGER (0/1) for the same
portability reason.
"""

from datetime import UTC, datetime

from alembic import op

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create retention tables, the purge index, and seed the system default."""
    # -- retention_policies: one row per tenant; tenant_id='default' is the
    #    system-wide fallback consulted when a tenant has no explicit policy.
    op.execute("""
        CREATE TABLE IF NOT EXISTS retention_policies (
            tenant_id TEXT PRIMARY KEY,
            audit_ttl_seconds INTEGER,
            run_ttl_seconds INTEGER,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    # -- legal_holds: run_id NULL == tenant-wide hold. A hold blocks BOTH TTL
    #    purge and explicit right-to-erasure while ``active`` is 1.
    op.execute("""
        CREATE TABLE IF NOT EXISTS legal_holds (
            hold_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            run_id TEXT,
            reason TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            placed_by TEXT,
            created_at TEXT NOT NULL,
            released_at TEXT
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_legal_holds_active
            ON legal_holds(tenant_id, active, run_id)
    """)

    # -- retention_audit_log: append-only record of every erasure/hold step.
    op.execute("""
        CREATE TABLE IF NOT EXISTS retention_audit_log (
            log_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            run_id TEXT,
            action TEXT NOT NULL,
            reason TEXT,
            detail TEXT,
            created_at TEXT NOT NULL
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_retention_audit_log_tenant
            ON retention_audit_log(tenant_id, created_at)
    """)

    # -- node_audits purge index: supports the tenant + created_at<cutoff scan
    #    that AuditRepository.list_erasable performs on every purge sweep.
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_node_audits_tenant_created
            ON node_audits(tenant_id, created_at)
    """)

    # Seed the system-default policy. TTLs NULL == keep forever (no automatic
    # purge) until an operator configures them via PUT /v1/retention/policy.
    now = datetime.now(UTC).isoformat()
    op.execute(
        "INSERT INTO retention_policies "
        "(tenant_id, audit_ttl_seconds, run_ttl_seconds, enabled, created_at, updated_at) "
        f"VALUES ('default', NULL, NULL, 1, '{now}', '{now}')"
    )


def downgrade() -> None:
    """Drop the retention tables and the node_audits purge index."""
    op.execute("DROP INDEX IF EXISTS idx_node_audits_tenant_created")
    op.execute("DROP INDEX IF EXISTS idx_retention_audit_log_tenant")
    op.execute("DROP TABLE IF EXISTS retention_audit_log")
    op.execute("DROP INDEX IF EXISTS idx_legal_holds_active")
    op.execute("DROP TABLE IF EXISTS legal_holds")
    op.execute("DROP TABLE IF EXISTS retention_policies")
