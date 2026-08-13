"""Record execution cost and usage measurement provenance."""

from alembic import op
import sqlalchemy as sa

revision = "20260812_04"
down_revision = "20260811_05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "execution_events" not in sa.inspect(op.get_bind()).get_table_names():
        return
    with op.batch_alter_table("execution_events") as batch:
        batch.alter_column("token_cost_usd", existing_type=sa.Numeric(12, 4), nullable=True)
        batch.alter_column("tool_cost_usd", existing_type=sa.Numeric(12, 4), nullable=True)
        batch.alter_column("compute_cost_usd", existing_type=sa.Numeric(12, 4), nullable=True)
        batch.add_column(sa.Column("cost_measurement", sa.String(16), nullable=True))
        batch.add_column(
            sa.Column("usage_measurement", sa.String(16), nullable=False, server_default="unmeasured")
        )
    op.execute(
        "UPDATE execution_events SET cost_measurement = CASE "
        "WHEN token_cost_usd = 0 AND tool_cost_usd = 0 AND compute_cost_usd = 0 "
        "THEN 'unmeasured' ELSE 'measured' END"
    )
    with op.batch_alter_table("execution_events") as batch:
        batch.alter_column(
            "cost_measurement",
            existing_type=sa.String(16),
            nullable=False,
            server_default="unmeasured",
        )


def downgrade() -> None:
    if "execution_events" not in sa.inspect(op.get_bind()).get_table_names():
        return
    op.execute(
        "UPDATE execution_events SET token_cost_usd = 0 WHERE token_cost_usd IS NULL"
    )
    op.execute("UPDATE execution_events SET tool_cost_usd = 0 WHERE tool_cost_usd IS NULL")
    op.execute(
        "UPDATE execution_events SET compute_cost_usd = 0 WHERE compute_cost_usd IS NULL"
    )
    with op.batch_alter_table("execution_events") as batch:
        batch.drop_column("usage_measurement")
        batch.drop_column("cost_measurement")
        batch.alter_column("compute_cost_usd", existing_type=sa.Numeric(12, 4), nullable=False)
        batch.alter_column("tool_cost_usd", existing_type=sa.Numeric(12, 4), nullable=False)
        batch.alter_column("token_cost_usd", existing_type=sa.Numeric(12, 4), nullable=False)
