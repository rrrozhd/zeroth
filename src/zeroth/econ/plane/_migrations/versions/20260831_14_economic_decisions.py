"""Add immutable tenant-scoped economic decision history.

Revision ID: 20260831_14
Revises: 20260830_13
Create Date: 2026-08-31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260831_14"
down_revision = "20260830_13"
branch_labels = None
depends_on = None

_TABLE = "economic_decisions"
_SCHEDULE_TABLE = "decision_schedules"
_KEY_TABLE = "cloud_api_keys"
_SUBSCRIPTION_TABLE = "cloud_subscriptions"
_USAGE_TABLE = "cloud_usage_counters"


def _has_table() -> bool:
    return _TABLE in set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if not _has_table():
        op.create_table(
            _TABLE,
            sa.Column("decision_id", sa.String(length=40), primary_key=True),
            sa.Column("tenant_id", sa.String(length=128), nullable=False),
            sa.Column("evidence_digest", sa.String(length=64), nullable=False),
            sa.Column("workflow", sa.String(length=128), nullable=False),
            sa.Column("baseline_version", sa.String(length=128), nullable=False),
            sa.Column("candidate_version", sa.String(length=128), nullable=False),
            sa.Column("outcome_type", sa.String(length=64), nullable=False),
            sa.Column("verdict", sa.String(length=16), nullable=False),
            sa.Column("recommended_action", sa.String(length=32), nullable=False),
            sa.Column("report_json", sa.JSON(), nullable=False),
            sa.Column("evaluated_at", sa.DateTime(), nullable=False),
            sa.Column("evaluated_by", sa.String(length=128), nullable=False),
        )
        op.create_index("ix_economic_decisions_tenant_id", _TABLE, ["tenant_id"])
        op.create_index("ix_economic_decisions_workflow", _TABLE, ["workflow"])
        op.create_index("ix_economic_decisions_verdict", _TABLE, ["verdict"])
        op.create_index("ix_economic_decisions_evaluated_at", _TABLE, ["evaluated_at"])
        op.create_index(
            "uq_economic_decisions_tenant_digest",
            _TABLE,
            ["tenant_id", "evidence_digest"],
            unique=True,
        )
        op.create_index(
            "ix_economic_decisions_tenant_workflow_time",
            _TABLE,
            ["tenant_id", "workflow", "evaluated_at"],
        )
    if _SCHEDULE_TABLE not in set(sa.inspect(op.get_bind()).get_table_names()):
        op.create_table(
            _SCHEDULE_TABLE,
            sa.Column("schedule_id", sa.String(length=40), primary_key=True),
            sa.Column("tenant_id", sa.String(length=128), nullable=False),
            sa.Column("workflow", sa.String(length=128), nullable=False),
            sa.Column("baseline_version", sa.String(length=128), nullable=False),
            sa.Column("candidate_version", sa.String(length=128), nullable=False),
            sa.Column("outcome_type", sa.String(length=64), nullable=False),
            sa.Column("policy_json", sa.JSON(), nullable=False),
            sa.Column("interval_minutes", sa.Integer(), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False),
            sa.Column("next_run_at", sa.DateTime(), nullable=False),
            sa.Column("last_run_at", sa.DateTime(), nullable=True),
            sa.Column("last_decision_id", sa.String(length=40), nullable=True),
            sa.Column("last_error", sa.String(length=512), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("created_by", sa.String(length=128), nullable=False),
        )
        op.create_index("ix_decision_schedules_tenant_id", _SCHEDULE_TABLE, ["tenant_id"])
        op.create_index("ix_decision_schedules_workflow", _SCHEDULE_TABLE, ["workflow"])
        op.create_index("ix_decision_schedules_next_run_at", _SCHEDULE_TABLE, ["next_run_at"])
        op.create_index(
            "ix_decision_schedules_tenant_due",
            _SCHEDULE_TABLE,
            ["tenant_id", "active", "next_run_at"],
        )
    if _KEY_TABLE not in set(sa.inspect(op.get_bind()).get_table_names()):
        op.create_table(
            _KEY_TABLE,
            sa.Column("key_id", sa.String(length=40), primary_key=True),
            sa.Column("tenant_id", sa.String(length=128), nullable=False),
            sa.Column("workspace_id", sa.String(length=128), nullable=True),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("secret_hash", sa.String(length=64), nullable=False),
            sa.Column("last_four", sa.String(length=4), nullable=False),
            sa.Column("roles_json", sa.JSON(), nullable=False),
            sa.Column("created_by", sa.String(length=128), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("last_used_at", sa.DateTime(), nullable=True),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_cloud_api_keys_tenant_id", _KEY_TABLE, ["tenant_id"])
        op.create_index(
            "uq_cloud_api_keys_secret_hash", _KEY_TABLE, ["secret_hash"], unique=True
        )
        op.create_index(
            "ix_cloud_api_keys_tenant_created",
            _KEY_TABLE,
            ["tenant_id", "created_at"],
        )
    if _SUBSCRIPTION_TABLE not in set(sa.inspect(op.get_bind()).get_table_names()):
        op.create_table(
            _SUBSCRIPTION_TABLE,
            sa.Column("tenant_id", sa.String(length=128), primary_key=True),
            sa.Column("plan", sa.String(length=32), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("period_start", sa.DateTime(), nullable=False),
            sa.Column("period_end", sa.DateTime(), nullable=False),
            sa.Column("external_customer_id", sa.String(length=128), nullable=True),
            sa.Column("external_subscription_id", sa.String(length=128), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "ix_cloud_subscriptions_status", _SUBSCRIPTION_TABLE, ["status"]
        )
    if _USAGE_TABLE not in set(sa.inspect(op.get_bind()).get_table_names()):
        op.create_table(
            _USAGE_TABLE,
            sa.Column("tenant_id", sa.String(length=128), primary_key=True),
            sa.Column("period_start", sa.DateTime(), primary_key=True),
            sa.Column("meter", sa.String(length=32), primary_key=True),
            sa.Column("quantity", sa.Integer(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if _USAGE_TABLE in tables:
        op.drop_table(_USAGE_TABLE)
    if _SUBSCRIPTION_TABLE in tables:
        op.drop_table(_SUBSCRIPTION_TABLE)
    if _KEY_TABLE in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table(_KEY_TABLE)
    if _SCHEDULE_TABLE in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table(_SCHEDULE_TABLE)
    if _has_table():
        op.drop_table(_TABLE)
