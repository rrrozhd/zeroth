"""Add webhook tables and approval SLA columns.

Revision ID: 003
Revises: 002
Create Date: 2026-04-07

Creates webhook_subscriptions, webhook_deliveries, and webhook_dead_letters
tables. Adds sla_deadline, escalation_action, and escalated_from_id nullable
columns to the approvals table for SLA timeout enforcement.
"""

from alembic import op

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create webhook tables and add SLA columns to approvals."""

    # -- webhook_subscriptions --
    op.execute("""
        CREATE TABLE IF NOT EXISTS webhook_subscriptions (
            subscription_id TEXT PRIMARY KEY,
            deployment_ref TEXT NOT NULL,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            target_url TEXT NOT NULL,
            secret TEXT NOT NULL,
            event_types TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_webhook_subs_deployment
        ON webhook_subscriptions(deployment_ref, active)
    """)

    # -- webhook_deliveries --
    op.execute("""
        CREATE TABLE IF NOT EXISTS webhook_deliveries (
            delivery_id TEXT PRIMARY KEY,
            subscription_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            event_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 5,
            next_attempt_at TEXT NOT NULL,
            last_error TEXT,
            last_status_code INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (subscription_id) REFERENCES webhook_subscriptions(subscription_id)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_webhook_del_pending
        ON webhook_deliveries(status, next_attempt_at)
    """)

    # -- webhook_dead_letters --
    op.execute("""
        CREATE TABLE IF NOT EXISTS webhook_dead_letters (
            dead_letter_id TEXT PRIMARY KEY,
            delivery_id TEXT NOT NULL,
            subscription_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            event_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            attempt_count INTEGER NOT NULL,
            last_error TEXT,
            last_status_code INTEGER,
            created_at TEXT NOT NULL,
            dead_lettered_at TEXT NOT NULL
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_webhook_dl_subscription
        ON webhook_dead_letters(subscription_id, dead_lettered_at DESC)
    """)

    # -- Add SLA columns to approvals (all nullable for backward compatibility) --
    op.execute("ALTER TABLE approvals ADD COLUMN sla_deadline TEXT")
    op.execute("ALTER TABLE approvals ADD COLUMN escalation_action TEXT")
    op.execute("ALTER TABLE approvals ADD COLUMN escalated_from_id TEXT")


def downgrade() -> None:
    """Remove webhook tables and SLA columns from approvals."""
    op.execute("DROP TABLE IF EXISTS webhook_dead_letters")
    op.execute("DROP TABLE IF EXISTS webhook_deliveries")
    op.execute("DROP TABLE IF EXISTS webhook_subscriptions")
    # SQLite does not support DROP COLUMN before 3.35; for production Postgres
    # this would work. Left as documentation of intent.
    # op.drop_column("approvals", "sla_deadline")
    # op.drop_column("approvals", "escalation_action")
    # op.drop_column("approvals", "escalated_from_id")
