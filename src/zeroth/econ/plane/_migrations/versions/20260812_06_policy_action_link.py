"""Link each policy action to the enforcement action that proposed it.

Revision ID: 20260812_06
Revises: 20260812_04
Create Date: 2026-08-12
"""

from __future__ import annotations

import sqlalchemy as sa

from zeroth.econ.plane._migrations.errors import DuplicatePolicyActionLink
from alembic import op

revision = "20260812_06"
down_revision = "20260812_04"
branch_labels = None
depends_on = None

_TABLE = "policy_actions"
_COLUMN = "enforcement_action_id"
_TARGET_TABLE = "enforcement_actions"
_TARGET_COLUMN = "id"
_UNIQUE_INDEX = "uq_policy_actions_enforcement_action_id"
_MAX_REPORTED_COLLISIONS = 20


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
        "foreign_key": (_TARGET_TABLE, _TARGET_COLUMN),
        "foreign_key_requires_target_table": True,
        "unique_index": _UNIQUE_INDEX,
        "unique": True,
    }


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _has_table() -> bool:
    return _TABLE in set(_inspector().get_table_names())


def _has_target_table() -> bool:
    return _TARGET_TABLE in set(_inspector().get_table_names())


def _has_column() -> bool:
    return _COLUMN in {column["name"] for column in _inspector().get_columns(_TABLE)}


def _index_names() -> set[str]:
    # A plain single-column index, unlike revision 20260812_07's expression
    # index, reflects on both dialects -- so the inspector is enough here.
    return {index["name"] for index in _inspector().get_indexes(_TABLE)}


def _add_link_column(*, with_foreign_key: bool) -> None:
    """Add the link column, carrying the foreign key exactly when it can hold.

    ``enforcement_actions`` is not part of this Alembic chain -- the runtime
    schema comes from ``create_all`` -- so a database built by the chain alone
    may not have the target table at all.  Declaring a reference to a table that
    does not exist is not integrity, it is a constraint that can never be
    checked; so the reference is added when the target is there and left out,
    visibly, when it is not.  Every real database has it: ``bootstrap()`` runs
    ``create_all`` before any convergence, and ``create_all`` builds
    ``enforcement_actions`` from ``EnforcementAction``.
    """
    if not with_foreign_key:
        with op.batch_alter_table(_TABLE) as batch_op:
            batch_op.add_column(sa.Column(_COLUMN, sa.Integer(), nullable=True))
        return
    if op.get_bind().dialect.name == "sqlite":
        # Alembic emits a separate ALTER TABLE ... ADD CONSTRAINT for a column
        # carrying a ForeignKey, which SQLite rejects outright; its documented
        # alternative is the copy-and-move batch rebuild, which would rewrite
        # every row of an erasure-audited table on a startup path.  SQLite does
        # accept the reference inline in ADD COLUMN -- one statement, no data
        # movement -- so emit exactly that.
        op.execute(
            f"ALTER TABLE {_TABLE} ADD COLUMN {_COLUMN} INTEGER "
            f"REFERENCES {_TARGET_TABLE}({_TARGET_COLUMN})"
        )
        return
    op.add_column(
        _TABLE,
        sa.Column(
            _COLUMN,
            sa.Integer(),
            sa.ForeignKey(f"{_TARGET_TABLE}.{_TARGET_COLUMN}"),
            nullable=True,
        ),
    )


def _preflight_duplicate_links() -> None:
    """Refuse before any DDL if the link is already ambiguous.

    ``_linked_policy_action`` resolves the link with ``scalar_one_or_none()``:
    a second policy action against one enforcement action does not degrade the
    decision, it makes the action permanently undecidable behind a 500.  The
    unique index is what turns that into a rejected write at the source -- but
    it cannot be built over rows that already collide, and the only automatic
    way through would be deleting one.  Name the collisions and stop, as
    revision 20260812_07 does for outcome identities.

    Naming them is safe here in a way it is not there: this is the enforcement
    action's own surrogate id, not an erasure subject key.
    """
    collisions = op.get_bind().execute(
        sa.text(
            f"SELECT {_COLUMN}, COUNT(*) AS row_count FROM {_TABLE} "
            f"WHERE {_COLUMN} IS NOT NULL GROUP BY {_COLUMN} "
            f"HAVING COUNT(*) > 1 ORDER BY {_COLUMN}"
        )
    ).all()
    if not collisions:
        return
    reported = collisions[:_MAX_REPORTED_COLLISIONS]
    details = ", ".join(
        f"{enforcement_action_id} ({row_count} rows)"
        for enforcement_action_id, row_count in reported
    )
    if len(collisions) > len(reported):
        details += f", ... ({len(collisions) - len(reported)} more)"
    raise DuplicatePolicyActionLink(
        "cannot make the policy-action link unique: "
        f"{len(collisions)} enforcement action(s) carry more than one policy action: {details}"
    )


def upgrade() -> None:
    migration_plan(op.get_bind().dialect.name)
    if not _has_table():
        return
    if not _has_column():
        # Nullable, no server default, and deliberately no backfill.  An existing
        # row carries nothing that identifies the enforcement action it was
        # proposed for -- that absence is the defect (A01-11), and the only
        # available heuristic, "the newest policy action for this capability", is
        # the exact rule being removed.  NULL therefore means "unlinked" and the
        # service leaves such rows alone.
        _add_link_column(with_foreign_key=_has_target_table())
    if _UNIQUE_INDEX not in _index_names():
        # Nullable-unique: NULL never collides with NULL on either dialect, so
        # every unlinked legacy row stays legal and only a genuine second link
        # to one enforcement action is rejected.
        _preflight_duplicate_links()
        op.create_index(_UNIQUE_INDEX, _TABLE, [_COLUMN], unique=True)


def downgrade() -> None:
    if not _has_table():
        return
    if _UNIQUE_INDEX in _index_names():
        op.drop_index(_UNIQUE_INDEX, table_name=_TABLE)
    if _has_column():
        with op.batch_alter_table(_TABLE) as batch_op:
            batch_op.drop_column(_COLUMN)
