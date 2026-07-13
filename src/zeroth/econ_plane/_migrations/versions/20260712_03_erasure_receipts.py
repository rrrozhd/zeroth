"""Add durable econ erasure operation receipts."""

from alembic import op
import sqlalchemy as sa

revision = "20260712_03"
down_revision = "20260226_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "econ_erasure_receipts",
        sa.Column("operation_id", sa.String(length=128), primary_key=True),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("deleted_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_econ_erasure_receipts_tenant_id",
        "econ_erasure_receipts",
        ["tenant_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_econ_erasure_receipts_tenant_id", table_name="econ_erasure_receipts")
    op.drop_table("econ_erasure_receipts")
