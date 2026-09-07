"""Give each declared physical charge one tenant-scoped execution owner."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260906_20"
down_revision = "20260906_19"
branch_labels = None
depends_on = None

_TABLE = "execution_events"
_INDEX = "uq_execution_events_tenant_charge_id"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in inspector.get_table_names():
        return
    present = {column["name"] for column in inspector.get_columns(_TABLE)}
    for name, length in (("cost_role", 32), ("charge_id", 128)):
        if name not in present:
            op.add_column(_TABLE, sa.Column(name, sa.String(length), nullable=True))
    if _INDEX not in {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(_TABLE)}:
        op.create_index(_INDEX, _TABLE, ["tenant_id", "charge_id"], unique=True)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in inspector.get_table_names():
        return
    present = {column["name"] for column in inspector.get_columns(_TABLE)}
    columns = [name for name in ("cost_role", "charge_id") if name in present]
    conditions = []
    if "cost_role" in columns:
        conditions.append("cost_role IS NOT NULL AND cost_role != 'legacy_unknown'")
    if "charge_id" in columns:
        conditions.append("charge_id IS NOT NULL")
    if conditions and op.get_bind().execute(sa.text(
        "SELECT 1 FROM execution_events WHERE " + " OR ".join(conditions) + " LIMIT 1"
    )).first() is not None:
        raise RuntimeError("Cannot discard charge ownership; use a compatible reader rollback")
    if _INDEX in {index["name"] for index in inspector.get_indexes(_TABLE)}:
        op.drop_index(_INDEX, table_name=_TABLE)
    for name in reversed(columns):
        op.drop_column(_TABLE, name)
