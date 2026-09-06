"""Append-only replacement cost assertions for existing charge owners."""

from alembic import op
import sqlalchemy as sa

revision = "20260906_22"
down_revision = "20260906_21"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = sa.inspect(op.get_bind()).get_table_names()
    # The economic chain can run without the runtime-owned execution table.
    # In that topology bootstrap creates both parent and child from metadata;
    # there are no charge owners to revise until it does.
    if "execution_events" not in tables or "charge_cost_revisions" in tables:
        return
    amount_type = sa.Numeric(18, 8).with_variant(sa.String(20), "sqlite")
    op.create_table(
        "charge_cost_revisions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("charge_id", sa.String(128), nullable=False),
        sa.Column("asserted_at", sa.DateTime(), nullable=False),
        sa.Column("ingested_at", sa.DateTime(), nullable=False),
        sa.Column("token_cost_usd", amount_type, nullable=True),
        sa.Column("tool_cost_usd", amount_type, nullable=True),
        sa.Column("compute_cost_usd", amount_type, nullable=True),
        sa.Column("cost_measurement", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(256), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "charge_id"], ["execution_events.tenant_id", "execution_events.charge_id"],
            name="fk_charge_cost_revision_owner", ondelete="CASCADE",
        ),
        sa.UniqueConstraint("tenant_id", "charge_id", "asserted_at", name="uq_charge_cost_revision_identity"),
    )


def downgrade() -> None:
    if "charge_cost_revisions" not in sa.inspect(op.get_bind()).get_table_names():
        return
    if op.get_bind().execute(sa.text("SELECT 1 FROM charge_cost_revisions LIMIT 1")).first():
        raise RuntimeError("Cannot discard charge cost revisions; use a compatible reader rollback")
    op.drop_table("charge_cost_revisions")
