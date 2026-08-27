"""Add ZER-37 GitHub App integration tables.

Revision ID: 033
Revises: 032
Create Date: 2026-08-26

Schema only, for the service-layer GitHub App integration:

* ``github_installations`` -- one row per GitHub App installation a tenant
  tracks. ``installation_id`` (GitHub's numeric id) is unique per tenant;
  lifecycle status is PENDING_CLAIM/ACTIVE/SUSPENDED/REVOKED.
* ``github_repositories`` -- one row per repository grant reachable through
  an installation, keyed to the installation row by ``installation_pk``.
* ``github_webhook_deliveries`` -- webhook delivery GUID dedup ledger, keyed
  per tenant so a replayed delivery is a no-op.

Raw ``op.execute`` keeps the DDL portable across SQLite and Postgres,
matching the 001-026 convention. Booleans follow the INTEGER portability
rule used throughout this migration chain.
"""

from alembic import op

revision = "033"
down_revision = "032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the three GitHub integration tables and their indexes."""
    op.execute("""
        CREATE TABLE IF NOT EXISTS github_installations (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            installation_id INTEGER NOT NULL,
            account_login TEXT NOT NULL,
            account_type TEXT NOT NULL,
            repository_selection TEXT NOT NULL,
            status TEXT NOT NULL,
            last_verified_at TEXT,
            suspended_at TEXT,
            revoked_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    op.create_index(
        "uq_github_installations_tenant_installation",
        "github_installations",
        ["tenant_id", "installation_id"],
        unique=True,
    )

    op.execute("""
        CREATE TABLE IF NOT EXISTS github_repositories (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            installation_pk TEXT NOT NULL,
            repo_id INTEGER NOT NULL,
            owner TEXT NOT NULL,
            name TEXT NOT NULL,
            full_name TEXT NOT NULL,
            private INTEGER NOT NULL DEFAULT 0,
            default_branch TEXT NOT NULL,
            status TEXT NOT NULL,
            added_at TEXT NOT NULL,
            removed_at TEXT,
            FOREIGN KEY (installation_pk) REFERENCES github_installations (id)
        )
    """)
    op.create_index(
        "uq_github_repositories_tenant_installation_repo",
        "github_repositories",
        ["tenant_id", "installation_pk", "repo_id"],
        unique=True,
    )

    op.execute("""
        CREATE TABLE IF NOT EXISTS github_webhook_deliveries (
            tenant_id TEXT NOT NULL DEFAULT 'default',
            delivery_guid TEXT NOT NULL,
            event TEXT NOT NULL,
            action TEXT,
            installation_id INTEGER,
            received_at TEXT NOT NULL,
            handled INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (tenant_id, delivery_guid)
        )
    """)
    op.create_index(
        "idx_github_webhook_deliveries_tenant_received",
        "github_webhook_deliveries",
        ["tenant_id", "received_at"],
    )


def downgrade() -> None:
    """Drop the three GitHub integration tables and their indexes."""
    op.drop_index(
        "idx_github_webhook_deliveries_tenant_received",
        table_name="github_webhook_deliveries",
    )
    op.execute("DROP TABLE IF EXISTS github_webhook_deliveries")

    op.drop_index(
        "uq_github_repositories_tenant_installation_repo",
        table_name="github_repositories",
    )
    op.execute("DROP TABLE IF EXISTS github_repositories")

    op.drop_index(
        "uq_github_installations_tenant_installation",
        table_name="github_installations",
    )
    op.execute("DROP TABLE IF EXISTS github_installations")
