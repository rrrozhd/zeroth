"""Add WorkOS organization and identity bindings for self-serve activation.

Revision ID: 20260901_17
Revises: 20260901_16
Create Date: 2026-09-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260901_17"
down_revision = "20260901_16"
branch_labels = None
depends_on = None

_TENANTS = "cloud_tenant_bindings"
_MEMBERSHIPS = "cloud_identity_memberships"


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if _TENANTS not in tables:
        op.create_table(
            _TENANTS,
            sa.Column("local_tenant_id", sa.String(length=128), primary_key=True),
            sa.Column("provider", sa.String(length=32), nullable=False),
            sa.Column("external_organization_id", sa.String(length=128), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "provider",
                "external_organization_id",
                name="uq_cloud_tenant_bindings_provider_org",
            ),
        )
    if _MEMBERSHIPS not in tables:
        op.create_table(
            _MEMBERSHIPS,
            sa.Column("tenant_id", sa.String(length=128), primary_key=True),
            sa.Column("provider", sa.String(length=32), primary_key=True),
            sa.Column("external_user_id", sa.String(length=128), primary_key=True),
            sa.Column("external_organization_id", sa.String(length=128), nullable=False),
            sa.Column("email", sa.String(length=255), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "ix_cloud_identity_memberships_provider_org",
            _MEMBERSHIPS,
            ["provider", "external_organization_id"],
        )


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if _MEMBERSHIPS in tables:
        op.drop_table(_MEMBERSHIPS)
    if _TENANTS in tables:
        op.drop_table(_TENANTS)
