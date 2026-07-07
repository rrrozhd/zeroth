"""connector outbox and delivery tables

Revision ID: 20260226_02
Revises: 20260223_01
Create Date: 2026-02-26
"""

from alembic import op
import sqlalchemy as sa


revision = "20260226_02"
down_revision = "20260223_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "connector_configs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(length=128), nullable=False, server_default="tenant_default"),
        sa.Column("connector_type", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_connector_configs_tenant_type_enabled", "connector_configs", ["tenant_id", "connector_type", "enabled"])

    op.create_table(
        "connector_outbox",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(length=128), nullable=False, server_default="tenant_default"),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("event_key", sa.String(length=255), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="PENDING"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=False),
        sa.Column("last_error", sa.String(length=1024), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("tenant_id", "event_type", "event_key", name="uq_connector_outbox_tenant_event_key"),
    )
    op.create_index("ix_connector_outbox_status_next_attempt", "connector_outbox", ["status", "next_attempt_at"])
    op.create_index("ix_connector_outbox_tenant_created", "connector_outbox", ["tenant_id", "created_at"])

    op.create_table(
        "connector_delivery_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("outbox_id", sa.Integer(), nullable=False),
        sa.Column("connector_type", sa.String(length=64), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("response_excerpt", sa.String(length=512), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_connector_delivery_log_outbox_id", "connector_delivery_log", ["outbox_id"])
    op.create_index("ix_connector_delivery_log_connector_type", "connector_delivery_log", ["connector_type"])


def downgrade() -> None:
    op.drop_index("ix_connector_delivery_log_connector_type", table_name="connector_delivery_log")
    op.drop_index("ix_connector_delivery_log_outbox_id", table_name="connector_delivery_log")
    op.drop_table("connector_delivery_log")

    op.drop_index("ix_connector_outbox_tenant_created", table_name="connector_outbox")
    op.drop_index("ix_connector_outbox_status_next_attempt", table_name="connector_outbox")
    op.drop_table("connector_outbox")

    op.drop_index("ix_connector_configs_tenant_type_enabled", table_name="connector_configs")
    op.drop_table("connector_configs")
