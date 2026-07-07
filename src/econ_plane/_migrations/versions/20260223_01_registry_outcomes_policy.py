"""registry, outcomes v2, policy actions

Revision ID: 20260223_01
Revises: 
Create Date: 2026-02-23
"""

from alembic import op
import sqlalchemy as sa


revision = "20260223_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "deployments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(length=128), nullable=False, server_default="tenant_default"),
        sa.Column("capability_id", sa.String(length=128), nullable=False),
        sa.Column("weights_json", sa.JSON(), nullable=False),
        sa.Column("routing_mode", sa.String(length=32), nullable=False, server_default="STATIC"),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "deployment_implementations",
        sa.Column("deployment_id", sa.Integer(), nullable=False),
        sa.Column("implementation_id", sa.String(length=128), nullable=False),
        sa.PrimaryKeyConstraint("deployment_id", "implementation_id"),
    )
    op.create_table(
        "experiments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(length=128), nullable=False, server_default="tenant_default"),
        sa.Column("capability_id", sa.String(length=128), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("impl_a_id", sa.String(length=128), nullable=False),
        sa.Column("impl_b_id", sa.String(length=128), nullable=False),
        sa.Column("start_at", sa.DateTime(), nullable=False),
        sa.Column("end_at", sa.DateTime(), nullable=True),
        sa.Column("target_pct", sa.Float(), nullable=False, server_default="50"),
        sa.Column("assignment_key", sa.String(length=32), nullable=False, server_default="request_id"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="ACTIVE"),
        sa.Column("constraints_json", sa.JSON(), nullable=False),
    )
    op.create_table(
        "policy_actions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(length=128), nullable=False, server_default="tenant_default"),
        sa.Column("capability_id", sa.String(length=128), nullable=False),
        sa.Column("proposed_at", sa.DateTime(), nullable=False),
        sa.Column("proposed_by", sa.String(length=128), nullable=False),
        sa.Column("action_type", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("metrics_snapshot_id", sa.Integer(), nullable=True),
        sa.Column("confidence_state_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("approved_by", sa.String(length=128), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.Column("failure_reason", sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("policy_actions")
    op.drop_table("experiments")
    op.drop_table("deployment_implementations")
    op.drop_table("deployments")
