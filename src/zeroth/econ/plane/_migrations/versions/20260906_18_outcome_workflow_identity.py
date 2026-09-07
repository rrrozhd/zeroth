"""Retain public outcome workflow identity without rewriting legacy registry IDs."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260906_18"
down_revision = "20260901_17"
branch_labels = None
depends_on = None

_TABLE = "outcome_events"
_COLUMNS = ("workflow_id", "workflow_version")
_INDEX = "ix_outcome_events_tenant_workflow_version"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in inspector.get_table_names():
        return
    present = {column["name"] for column in inspector.get_columns(_TABLE)}
    for name in _COLUMNS:
        if name not in present:
            op.add_column(_TABLE, sa.Column(name, sa.String(length=128), nullable=True))
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(_TABLE)}
    if _INDEX not in indexes:
        op.create_index(_INDEX, _TABLE, ["tenant_id", *_COLUMNS], unique=False)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in inspector.get_table_names():
        return
    present = {column["name"] for column in inspector.get_columns(_TABLE)}
    columns = [name for name in _COLUMNS if name in present]
    if columns:
        populated = (
            op.get_bind()
            .execute(
                sa.text(
                    "SELECT 1 FROM outcome_events WHERE "
                    + " OR ".join(f"{name} IS NOT NULL" for name in columns)
                    + " LIMIT 1"
                )
            )
            .first()
        )
        if populated is not None:
            raise RuntimeError(
                "Cannot discard retained workflow identity; use a compatible reader rollback"
            )
    if _INDEX in {index["name"] for index in inspector.get_indexes(_TABLE)}:
        op.drop_index(_INDEX, table_name=_TABLE)
    for name in reversed(columns):
        op.drop_column(_TABLE, name)
