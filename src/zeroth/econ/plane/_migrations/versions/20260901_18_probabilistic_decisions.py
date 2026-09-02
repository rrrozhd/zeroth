"""Add immutable probabilistic model-migration evidence and decision history.

Revision ID: 20260901_18
Revises: 20260901_17
Create Date: 2026-09-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260901_18"
down_revision = "20260901_17"
branch_labels = None
depends_on = None

_TABLE = "probabilistic_migration_decisions"


def upgrade() -> None:
    if _TABLE in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        _TABLE,
        sa.Column("decision_id", sa.String(length=40), primary_key=True),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("workload", sa.String(length=128), nullable=False),
        sa.Column("incumbent_model", sa.String(length=255), nullable=False),
        sa.Column("candidate_model", sa.String(length=255), nullable=False),
        sa.Column("verdict", sa.String(length=16), nullable=False),
        sa.Column("recommended_action", sa.String(length=32), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("policy_json", sa.JSON(), nullable=False),
        sa.Column("report_json", sa.JSON(), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(), nullable=False),
        sa.Column("evaluated_by", sa.String(length=128), nullable=False),
    )
    op.create_index(
        "ix_probabilistic_migration_decisions_tenant_id", _TABLE, ["tenant_id"]
    )
    op.create_index("ix_probabilistic_migration_decisions_workload", _TABLE, ["workload"])
    op.create_index("ix_probabilistic_migration_decisions_verdict", _TABLE, ["verdict"])
    op.create_index(
        "ix_probabilistic_migration_decisions_evaluated_at", _TABLE, ["evaluated_at"]
    )
    op.create_index(
        "uq_probabilistic_migration_decisions_tenant_digest",
        _TABLE,
        ["tenant_id", "request_digest"],
        unique=True,
    )
    op.create_index(
        "ix_probabilistic_migration_decisions_tenant_workload_time",
        _TABLE,
        ["tenant_id", "workload", "evaluated_at"],
    )


def downgrade() -> None:
    if _TABLE in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table(_TABLE)
