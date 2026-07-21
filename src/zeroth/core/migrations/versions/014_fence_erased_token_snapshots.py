"""Durably fence token snapshot writes after run erasure."""

import sqlalchemy as sa
from alembic import op

revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "token_snapshot_write_disabled",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch_op:
        batch_op.drop_column("token_snapshot_write_disabled")
