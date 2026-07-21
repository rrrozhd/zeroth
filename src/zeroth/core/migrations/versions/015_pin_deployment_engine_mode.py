"""Pin deployment engine mode and version attestation payloads."""

import sqlalchemy as sa
from alembic import op

revision = "015"
down_revision = "014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "deployment_versions",
        sa.Column("engine_mode", sa.String(), nullable=False, server_default="legacy"),
    )
    op.add_column(
        "deployment_versions",
        sa.Column(
            "attestation_payload_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )


def downgrade() -> None:
    with op.batch_alter_table("deployment_versions") as batch_op:
        batch_op.drop_column("attestation_payload_version")
        batch_op.drop_column("engine_mode")
