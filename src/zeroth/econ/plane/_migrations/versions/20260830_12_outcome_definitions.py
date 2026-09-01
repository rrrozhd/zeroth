"""Add immutable workflow-version outcome definitions."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260830_12"
down_revision = "20260830_11"
branch_labels = None
depends_on = None

_TABLE = "outcome_definitions"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _TABLE in inspector.get_table_names():
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("workflow_id", sa.String(length=192), nullable=False),
        sa.Column("workflow_version", sa.String(length=192), nullable=False),
        sa.Column("outcome_type", sa.String(length=64), nullable=False),
        sa.Column("operator", sa.String(length=32), nullable=False),
        sa.Column("target_json", sa.JSON(), nullable=False),
        sa.Column("definition_digest", sa.String(length=71), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "workflow_id",
            "workflow_version",
            name="uq_outcome_definitions_tenant_workflow_version",
        ),
    )
    op.create_index(
        "ix_outcome_definitions_tenant_id", _TABLE, ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_outcome_definitions_workflow_id", _TABLE, ["workflow_id"], unique=False
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _TABLE in inspector.get_table_names():
        op.drop_table(_TABLE)
