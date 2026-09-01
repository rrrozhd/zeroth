"""Add immutable provider bill reconciliation records."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260830_13"
down_revision = "20260830_12"
branch_labels = None
depends_on = None

_BILLS = "provider_bills"
_BUCKETS = "provider_cost_buckets"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if _BILLS not in tables:
        op.create_table(
            _BILLS,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("tenant_id", sa.String(length=128), nullable=False),
            sa.Column("provider", sa.String(length=64), nullable=False),
            sa.Column("statement_id", sa.String(length=192), nullable=False),
            sa.Column("period_start", sa.DateTime(), nullable=False),
            sa.Column("period_end", sa.DateTime(), nullable=False),
            sa.Column("currency", sa.String(length=3), nullable=False),
            sa.Column("billed_total_usd", sa.Numeric(18, 8), nullable=False),
            sa.Column("source_kind", sa.String(length=32), nullable=False),
            sa.Column("statement_digest", sa.String(length=71), nullable=False),
            sa.Column("imported_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "tenant_id", "id", name="uq_provider_bills_tenant_id"
            ),
            sa.UniqueConstraint(
                "tenant_id",
                "provider",
                "statement_id",
                name="uq_provider_bills_tenant_provider_statement",
            ),
        )
        op.create_index("ix_provider_bills_tenant_id", _BILLS, ["tenant_id"])
        op.create_index("ix_provider_bills_provider", _BILLS, ["provider"])
        op.create_index("ix_provider_bills_statement_id", _BILLS, ["statement_id"])
    inspector = sa.inspect(op.get_bind())
    if _BUCKETS not in inspector.get_table_names():
        op.create_table(
            _BUCKETS,
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("tenant_id", sa.String(length=128), nullable=False),
            sa.Column("provider_bill_id", sa.Integer(), nullable=False),
            sa.Column("bucket_id", sa.String(length=192), nullable=False),
            sa.Column("period_start", sa.DateTime(), nullable=False),
            sa.Column("period_end", sa.DateTime(), nullable=False),
            sa.Column("amount_usd", sa.Numeric(18, 8), nullable=False),
            sa.Column("model", sa.String(length=128), nullable=True),
            sa.Column("provider_dimensions", sa.JSON(), nullable=False),
            sa.ForeignKeyConstraint(
                ["tenant_id", "provider_bill_id"],
                ["provider_bills.tenant_id", "provider_bills.id"],
                name="fk_provider_cost_buckets_tenant_bill",
            ),
            sa.UniqueConstraint(
                "tenant_id",
                "provider_bill_id",
                "bucket_id",
                name="uq_provider_cost_buckets_tenant_bill_bucket",
            ),
        )
        op.create_index(
            "ix_provider_cost_buckets_tenant_id", _BUCKETS, ["tenant_id"]
        )
        op.create_index(
            "ix_provider_cost_buckets_provider_bill_id",
            _BUCKETS,
            ["provider_bill_id"],
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if _BUCKETS in tables:
        op.drop_table(_BUCKETS)
    if _BILLS in tables:
        op.drop_table(_BILLS)
