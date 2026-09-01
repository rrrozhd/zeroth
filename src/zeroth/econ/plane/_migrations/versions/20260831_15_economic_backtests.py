"""Add immutable tenant-scoped hosted economic backtests.

Revision ID: 20260831_15
Revises: 20260831_14
Create Date: 2026-08-31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260831_15"
down_revision = "20260831_14"
branch_labels = None
depends_on = None

_TABLE = "economic_backtests"


def upgrade() -> None:
    if _TABLE in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        _TABLE,
        sa.Column("backtest_id", sa.String(length=40), primary_key=True),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("workflow", sa.String(length=128), nullable=False),
        sa.Column("baseline_version", sa.String(length=128), nullable=True),
        sa.Column("node_id", sa.String(length=128), nullable=True),
        sa.Column("incumbent_model", sa.String(length=255), nullable=True),
        sa.Column("candidate_model", sa.String(length=255), nullable=True),
        sa.Column("verdict", sa.String(length=16), nullable=False),
        sa.Column("provider_call_credits", sa.Integer(), nullable=False),
        sa.Column("report_json", sa.JSON(), nullable=False),
        sa.Column("period_start", sa.DateTime(), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(), nullable=False),
        sa.Column("evaluated_by", sa.String(length=128), nullable=False),
    )
    op.create_index("ix_economic_backtests_tenant_id", _TABLE, ["tenant_id"])
    op.create_index("ix_economic_backtests_workflow", _TABLE, ["workflow"])
    op.create_index("ix_economic_backtests_verdict", _TABLE, ["verdict"])
    op.create_index("ix_economic_backtests_evaluated_at", _TABLE, ["evaluated_at"])
    op.create_index(
        "uq_economic_backtests_tenant_digest",
        _TABLE,
        ["tenant_id", "request_digest"],
        unique=True,
    )
    op.create_index(
        "ix_economic_backtests_tenant_workflow_time",
        _TABLE,
        ["tenant_id", "workflow", "evaluated_at"],
    )


def downgrade() -> None:
    if _TABLE in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table(_TABLE)
