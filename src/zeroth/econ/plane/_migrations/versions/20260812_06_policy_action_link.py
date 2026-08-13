"""Link each policy action to the enforcement action that proposed it.

Revision ID: 20260812_06
Revises: 20260812_04
Create Date: 2026-08-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260812_06"
down_revision = "20260812_04"
branch_labels = None
depends_on = None

_TABLE = "policy_actions"
_COLUMN = "enforcement_action_id"


def migration_plan(dialect_name: str) -> dict[str, object]:
    """Return the invariant plan shared by SQLite and PostgreSQL execution."""
    if dialect_name not in {"sqlite", "postgresql"}:
        raise ValueError(f"unsupported policy-action link migration dialect: {dialect_name}")
    return {
        "table": _TABLE,
        "column": _COLUMN,
        "nullable": True,
        "server_default": None,
        "backfills_existing_rows": False,
    }


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _has_table() -> bool:
    return _TABLE in set(_inspector().get_table_names())


def _has_column() -> bool:
    return _COLUMN in {column["name"] for column in _inspector().get_columns(_TABLE)}


def upgrade() -> None:
    migration_plan(op.get_bind().dialect.name)
    if not _has_table() or _has_column():
        return
    # Nullable, no server default, and deliberately no backfill.  An existing row
    # carries nothing that identifies the enforcement action it was proposed for --
    # that absence is the defect (A01-11), and the only available heuristic, "the
    # newest policy action for this capability", is the exact rule being removed.
    # NULL therefore means "unlinked" and the service leaves such rows alone.
    #
    # No FOREIGN KEY constraint: enforcement_actions is not part of the
    # Alembic-managed econ schema (the runtime schema comes from create_all), so
    # the target table may legitimately be absent here.  This mirrors
    # policy_actions.metrics_snapshot_id, which is a plain Integer in revision
    # 20260223_01 while the ORM model declares the foreign key.
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.add_column(sa.Column(_COLUMN, sa.Integer(), nullable=True))


def downgrade() -> None:
    if not _has_table() or not _has_column():
        return
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_column(_COLUMN)
