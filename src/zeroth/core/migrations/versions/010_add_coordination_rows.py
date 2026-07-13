"""Add database-backed audit and retention coordination rows.

Revision ID: 010
Revises: 009
Create Date: 2026-07-12
"""

import sqlalchemy as sa
from alembic import op

revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None

_SEQUENCE_INDEX = "uq_node_audits_run_chain_sequence"


def upgrade() -> None:
    """Add coordination tables and stable per-run audit ordering."""
    op.execute("""
        CREATE TABLE IF NOT EXISTS audit_chain_heads (
            run_id TEXT PRIMARY KEY,
            head_digest TEXT,
            next_sequence INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS retention_coordination (
            tenant_id TEXT PRIMARY KEY,
            updated_at TEXT NOT NULL
        )
    """)
    op.add_column("node_audits", sa.Column("chain_sequence", sa.Integer(), nullable=True))

    # Existing records predate explicit sequence allocation. Preserve their
    # established repository ordering exactly, independently for each run.
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT audit_id, run_id FROM node_audits "
            "ORDER BY run_id, created_at, audit_id"
        )
    ).mappings()
    sequence_by_run: dict[str, int] = {}
    update = sa.text(
        "UPDATE node_audits SET chain_sequence = :chain_sequence WHERE audit_id = :audit_id"
    )
    for row in rows:
        run_id = row["run_id"]
        sequence = sequence_by_run.get(run_id, 0) + 1
        sequence_by_run[run_id] = sequence
        connection.execute(
            update,
            {"chain_sequence": sequence, "audit_id": row["audit_id"]},
        )

    op.create_index(
        _SEQUENCE_INDEX,
        "node_audits",
        ["run_id", "chain_sequence"],
        unique=True,
    )


def downgrade() -> None:
    """Remove coordination state while retaining all legacy audit records."""
    op.drop_index(_SEQUENCE_INDEX, table_name="node_audits")
    with op.batch_alter_table("node_audits") as batch_op:
        batch_op.drop_column("chain_sequence")
    op.execute("DROP TABLE IF EXISTS retention_coordination")
    op.execute("DROP TABLE IF EXISTS audit_chain_heads")
