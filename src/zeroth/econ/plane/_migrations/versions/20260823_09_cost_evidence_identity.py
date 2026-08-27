"""Add explicit deployment and evidence identity to cost records.

Revision ID: 20260823_09
Revises: 20260822_08
Create Date: 2026-08-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260823_09"
down_revision = "20260822_08"
branch_labels = None
depends_on = None

_TABLES = ("cost_reservations", "execution_events")


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    present_tables = set(inspector.get_table_names())
    for table in _TABLES:
        if table not in present_tables:
            continue
        columns = {column["name"] for column in inspector.get_columns(table)}
        if "deployment_ref" not in columns:
            op.add_column(table, sa.Column("deployment_ref", sa.String(192), nullable=True))
        if "evidence_kind" not in columns:
            op.add_column(
                table,
                sa.Column(
                    "evidence_kind",
                    sa.String(32),
                    nullable=False,
                    server_default="legacy_unknown",
                ),
            )
        op.execute(
            sa.text(
                f"UPDATE {table} SET evidence_kind = 'synthetic_control' "
                "WHERE operation_id LIKE 'control-gate:%'"
            )
        )
        index_name = f"ix_{table}_tenant_deployment"
        indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}
        if index_name not in indexes:
            op.create_index(index_name, table, ["tenant_id", "deployment_ref"], unique=False)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    present_tables = set(inspector.get_table_names())
    for table in reversed(_TABLES):
        if table not in present_tables:
            continue
        indexes = {index["name"] for index in inspector.get_indexes(table)}
        index_name = f"ix_{table}_tenant_deployment"
        if index_name in indexes:
            op.drop_index(index_name, table_name=table)
        columns = {column["name"] for column in inspector.get_columns(table)}
        if "evidence_kind" in columns:
            op.drop_column(table, "evidence_kind")
        if "deployment_ref" in columns:
            op.drop_column(table, "deployment_ref")
