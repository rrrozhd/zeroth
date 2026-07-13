"""Add WS-B tenant-scoping columns to graph_versions and memory_connector_configs.

Revision ID: 007
Revises: 006
Create Date: 2026-07-11

Defense-in-depth tenant isolation (single-tenant-per-deployment): deployments
that share a physical control-plane DB must not leak across tenants.

* ``graph_versions.tenant_id``           — owning tenant of a graph version.
  ``graph_id`` stays GLOBALLY unique, so this is a plain ADD COLUMN (no PK
  rebuild). A covering index supports the repository's tenant-scoped
  latest-version lookups.
* ``memory_connector_configs.tenant_id`` — owning tenant of a persisted
  connector config (its ``params`` carry DSNs/credentials). ADD COLUMN only;
  ``ref`` remains the PRIMARY KEY, so two tenants cannot persist the same ref
  on one shared DB — an accepted single-tenant-per-deployment limitation.

``NOT NULL DEFAULT 'default'`` backfills every existing row to the reserved
single-tenant sentinel automatically, so pre-migration graphs/configs stay
readable by a ``'default'``-tenant caller. Raw ``op.execute`` keeps the DDL
portable across SQLite and Postgres, matching the 001-005 convention.
"""

from alembic import op

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add tenant_id columns (backfilled to 'default') and the graph index."""
    op.execute("ALTER TABLE graph_versions ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'")
    op.execute(
        "ALTER TABLE memory_connector_configs ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_graph_versions_tenant "
        "ON graph_versions(tenant_id, graph_id, version DESC)"
    )


def downgrade() -> None:
    """Drop the tenant index and tenant_id columns."""
    op.execute("DROP INDEX IF EXISTS idx_graph_versions_tenant")
    op.drop_column("memory_connector_configs", "tenant_id")
    op.drop_column("graph_versions", "tenant_id")
