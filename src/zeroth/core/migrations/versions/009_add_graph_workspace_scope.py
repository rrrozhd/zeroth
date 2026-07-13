"""Add workspace ownership to graph versions.

Revision ID: 009
Revises: 008
Create Date: 2026-07-12
"""

import sqlalchemy as sa
from alembic import op

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None

_INDEX_NAME = "idx_graph_versions_tenant_workspace"


def upgrade() -> None:
    """Add nullable workspace ownership and its tenant/workspace lookup index."""
    op.add_column("graph_versions", sa.Column("workspace_id", sa.Text(), nullable=True))
    op.create_index(
        _INDEX_NAME,
        "graph_versions",
        ["tenant_id", "workspace_id"],
    )


def downgrade() -> None:
    """Remove workspace ownership using SQLite-compatible batch alteration."""
    op.drop_index(_INDEX_NAME, table_name="graph_versions")
    with op.batch_alter_table("graph_versions") as batch_op:
        batch_op.drop_column("workspace_id")
