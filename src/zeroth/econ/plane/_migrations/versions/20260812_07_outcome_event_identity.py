"""Give an outcome event the same durable identity an execution event has.

Revision ID: 20260812_07
Revises: 20260812_06
Create Date: 2026-08-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260812_07"
down_revision = "20260812_06"
branch_labels = None
depends_on = None

_TABLE = "outcome_events"
_INDEX = "uq_outcome_events_tenant_identity"
_IDENTITY_COLUMNS = ("tenant_id", "join_key", "outcome_type", "occurred_at")
_IMPLEMENTATION_COLUMN = "implementation_id"
_IMPLEMENTATION_EXPRESSION = "coalesce(implementation_id, '')"


def migration_plan(dialect_name: str) -> dict[str, object]:
    """Return the invariant plan shared by SQLite and PostgreSQL execution."""
    if dialect_name not in {"sqlite", "postgresql"}:
        raise ValueError(f"unsupported outcome-identity migration dialect: {dialect_name}")
    return {
        "table": _TABLE,
        "index": _INDEX,
        "identity_columns": _IDENTITY_COLUMNS,
        "implementation_expression": _IMPLEMENTATION_EXPRESSION,
        "unique": True,
        "refuses_existing_duplicates": True,
        "deletes_rows": False,
    }


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _has_table() -> bool:
    return _TABLE in set(_inspector().get_table_names())


def _has_identity_columns() -> bool:
    present = {column["name"] for column in _inspector().get_columns(_TABLE)}
    return set(_IDENTITY_COLUMNS + (_IMPLEMENTATION_COLUMN,)) <= present


def _index_names() -> set[str]:
    # Expression-based indexes are skipped by SQLAlchemy reflection on SQLite, so
    # names are read from the catalog rather than from get_indexes().
    if op.get_bind().dialect.name == "sqlite":
        rows = op.get_bind().execute(
            sa.text(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = :table"
            ),
            {"table": _TABLE},
        )
        return {row[0] for row in rows}
    return {index["name"] for index in _inspector().get_indexes(_TABLE)}


def _preflight_duplicate_identities() -> None:
    """Refuse before any DDL if the table already violates the new identity.

    CREATE UNIQUE INDEX fails outright on a table that already holds duplicates,
    and the only way to make it succeed automatically is to delete rows -- which
    is exactly what an erasure-audited econ table must never do behind the
    operator's back.  Name the collisions and stop, as revision 20260811_05 does
    for cross-tenant execution ids.
    """
    identity = ", ".join(_IDENTITY_COLUMNS) + f", {_IMPLEMENTATION_EXPRESSION}"
    collisions = op.get_bind().execute(
        sa.text(
            f"SELECT tenant_id, join_key, outcome_type, COUNT(*) AS row_count FROM {_TABLE} "
            f"GROUP BY {identity} HAVING COUNT(*) > 1 "
            "ORDER BY tenant_id, join_key, outcome_type"
        )
    ).all()
    if not collisions:
        return
    details = ", ".join(
        f"{tenant_id}/{join_key}/{outcome_type} ({row_count} rows)"
        for tenant_id, join_key, outcome_type, row_count in collisions
    )
    raise RuntimeError(
        "cannot make outcome identity unique: "
        f"{len(collisions)} duplicate outcome identit(ies): {details}"
    )


def upgrade() -> None:
    migration_plan(op.get_bind().dialect.name)
    # outcome_events is not created by this Alembic chain -- the runtime schema
    # comes from Base.metadata.create_all -- so the table, or a column of it, may
    # legitimately be absent here.  Same reasoning as 20260812_06's missing FK.
    if not _has_table() or not _has_identity_columns():
        return
    if _INDEX in _index_names():
        return
    _preflight_duplicate_identities()
    op.create_index(
        _INDEX,
        _TABLE,
        [
            *_IDENTITY_COLUMNS,
            sa.text(_IMPLEMENTATION_EXPRESSION),
        ],
        unique=True,
    )


def downgrade() -> None:
    if not _has_table() or _INDEX not in _index_names():
        return
    op.drop_index(_INDEX, table_name=_TABLE)
