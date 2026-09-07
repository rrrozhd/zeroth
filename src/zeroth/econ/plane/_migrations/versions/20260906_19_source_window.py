"""Bind received executions to caller-declared capture windows."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260906_19"
down_revision = "20260906_18"
branch_labels = None
depends_on = None

_TABLE = "execution_events"
_COLUMN = "source_window_id"
_INDEX = "ix_execution_events_tenant_source_window"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in inspector.get_table_names():
        return
    if _COLUMN not in {column["name"] for column in inspector.get_columns(_TABLE)}:
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(length=128), nullable=True))
    if _INDEX not in {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(_TABLE)}:
        op.create_index(_INDEX, _TABLE, ["tenant_id", _COLUMN], unique=False)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in inspector.get_table_names():
        return
    if _COLUMN not in {column["name"] for column in inspector.get_columns(_TABLE)}:
        return
    populated = op.get_bind().execute(sa.text(
        "SELECT 1 FROM execution_events WHERE source_window_id IS NOT NULL LIMIT 1"
    )).first()
    if populated is not None:
        raise RuntimeError("Cannot discard source window identity; use a compatible reader rollback")
    if _INDEX in {index["name"] for index in inspector.get_indexes(_TABLE)}:
        op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_column(_TABLE, _COLUMN)
