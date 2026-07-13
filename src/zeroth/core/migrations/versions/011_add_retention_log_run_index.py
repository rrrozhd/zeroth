"""Index retention cleanup event replay by run and timestamp."""

from alembic import op

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "idx_retention_audit_log_run",
        "retention_audit_log",
        ["run_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_retention_audit_log_run", table_name="retention_audit_log")
