"""Add persistent LangGraph decisions and capability evidence."""

import sqlalchemy as sa
from alembic import op

revision = "018"
down_revision = "017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "langgraph_decisions",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("deployment_ref", sa.String(), nullable=False),
        sa.Column("action_hash", sa.String(), nullable=False),
        sa.Column("response_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "idempotency_key"),
    )
    op.create_table(
        "langgraph_inventories",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("deployment_ref", sa.String(), nullable=False),
        sa.Column("graph_version", sa.String(), nullable=False),
        sa.Column("adapter_version", sa.String(), nullable=False),
        sa.Column("inventory_fingerprint", sa.String(), nullable=False),
        sa.Column("coverage", sa.String(), nullable=False),
        sa.Column("entries_json", sa.Text(), nullable=False),
        sa.Column("registered_at", sa.String(), nullable=False),
        sa.Column("heartbeat_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "deployment_ref",
            "graph_version",
            "adapter_version",
            "inventory_fingerprint",
        ),
    )
    op.create_table(
        "langgraph_run_attestations",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("deployment_ref", sa.String(), nullable=False),
        sa.Column("correlation_id", sa.String(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("signature", sa.Text(), nullable=False),
        sa.Column("signing_key_id", sa.String(), nullable=False),
        sa.Column("algorithm", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "deployment_ref", "correlation_id"),
    )


def downgrade() -> None:
    op.drop_table("langgraph_run_attestations")
    op.drop_table("langgraph_inventories")
    op.drop_table("langgraph_decisions")
