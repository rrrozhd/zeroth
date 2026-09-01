"""Add vendor-neutral billing event receipts and projection ordering.

Revision ID: 20260901_16
Revises: 20260831_15
Create Date: 2026-09-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260901_16"
down_revision = "20260831_15"
branch_labels = None
depends_on = None

_RECEIPTS = "billing_event_receipts"
_SUBSCRIPTIONS = "cloud_subscriptions"
_PROJECTION_COLUMNS = (
    ("billing_provider", sa.String(length=32)),
    ("external_price_id", sa.String(length=128)),
    ("last_billing_event_id", sa.String(length=128)),
    ("last_billing_event_at", sa.DateTime()),
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if _RECEIPTS not in tables:
        op.create_table(
            _RECEIPTS,
            sa.Column("tenant_id", sa.String(length=128), primary_key=True),
            sa.Column("provider", sa.String(length=32), primary_key=True),
            sa.Column("event_id", sa.String(length=128), primary_key=True),
            sa.Column("external_subscription_id", sa.String(length=128), nullable=False),
            sa.Column("payload_digest", sa.String(length=64), nullable=False),
            sa.Column("disposition", sa.String(length=32), nullable=False),
            sa.Column("occurred_at", sa.DateTime(), nullable=False),
            sa.Column("processed_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "ix_billing_receipts_tenant_subscription_time",
            _RECEIPTS,
            ["tenant_id", "external_subscription_id", "occurred_at"],
        )
    if _SUBSCRIPTIONS in tables:
        existing = {column["name"] for column in inspector.get_columns(_SUBSCRIPTIONS)}
        for name, column_type in _PROJECTION_COLUMNS:
            if name not in existing:
                op.add_column(
                    _SUBSCRIPTIONS,
                    sa.Column(name, column_type, nullable=True),
                )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if _RECEIPTS in tables:
        op.drop_table(_RECEIPTS)
    if _SUBSCRIPTIONS in tables:
        existing = {column["name"] for column in inspector.get_columns(_SUBSCRIPTIONS)}
        for name, _column_type in reversed(_PROJECTION_COLUMNS):
            if name in existing:
                op.drop_column(_SUBSCRIPTIONS, name)
