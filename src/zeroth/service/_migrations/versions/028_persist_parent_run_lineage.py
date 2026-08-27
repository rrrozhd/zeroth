"""Persist parent/child run lineage for composed execution."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "028"
down_revision = "027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("parent_run_id", sa.Text(), nullable=True))
    op.create_index(
        "idx_runs_parent",
        "runs",
        ["tenant_id", "workspace_scope", "parent_run_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_runs_parent", table_name="runs")
    op.drop_column("runs", "parent_run_id")
