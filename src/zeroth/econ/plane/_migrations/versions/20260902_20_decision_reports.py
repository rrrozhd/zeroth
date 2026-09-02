"""Add immutable decision report artifacts and email delivery audit rows.

Revision ID: 20260902_20
Revises: 20260902_19
Create Date: 2026-09-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260902_20"
down_revision = "20260902_19"
branch_labels = None
depends_on = None


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _table_names()
    if "decision_reports" not in tables:
        op.create_table(
            "decision_reports",
            sa.Column("report_id", sa.String(40), primary_key=True),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("decision_id", sa.String(40), nullable=False),
            sa.Column("template_version", sa.String(32), nullable=False),
            sa.Column("media_type", sa.String(64), nullable=False),
            sa.Column("sha256", sa.String(64), nullable=False),
            sa.Column("pdf_bytes", sa.LargeBinary(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("created_by", sa.String(128), nullable=False),
            sa.UniqueConstraint(
                "tenant_id",
                "decision_id",
                "template_version",
                name="uq_decision_report_version",
            ),
        )
        for column in ("tenant_id", "decision_id", "created_at"):
            op.create_index(f"ix_decision_reports_{column}", "decision_reports", [column])
        op.create_index(
            "ix_decision_reports_tenant_created",
            "decision_reports",
            ["tenant_id", "created_at"],
        )
    if "decision_report_deliveries" not in tables:
        op.create_table(
            "decision_report_deliveries",
            sa.Column("delivery_id", sa.String(40), primary_key=True),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("report_id", sa.String(40), nullable=False),
            sa.Column("report_sha256", sa.String(64), nullable=False),
            sa.Column("recipients_json", sa.JSON(), nullable=False),
            sa.Column("delivery_mode", sa.String(16), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("last_error", sa.String(512), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("sent_at", sa.DateTime(), nullable=True),
            sa.Column("created_by", sa.String(128), nullable=False),
        )
        for column in ("tenant_id", "report_id", "status", "created_at"):
            op.create_index(
                f"ix_decision_report_deliveries_{column}",
                "decision_report_deliveries",
                [column],
            )
        op.create_index(
            "ix_report_deliveries_tenant_created",
            "decision_report_deliveries",
            ["tenant_id", "created_at"],
        )


def downgrade() -> None:
    tables = _table_names()
    if "decision_report_deliveries" in tables:
        op.drop_table("decision_report_deliveries")
    if "decision_reports" in tables:
        op.drop_table("decision_reports")
