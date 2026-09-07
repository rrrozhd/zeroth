"""Preserve original cost assertions exactly on SQLite; PostgreSQL is already numeric."""

from alembic import op
import sqlalchemy as sa

revision = "20260906_23"
down_revision = "20260906_22"
branch_labels = None
depends_on = None

_TABLE = "execution_events"
_COSTS = ("token_cost_usd", "tool_cost_usd", "compute_cost_usd")


def _columns(bind):
    inspector = sa.inspect(bind)
    if bind.dialect.name != "sqlite" or _TABLE not in inspector.get_table_names():
        return {}
    return {c["name"]: c for c in inspector.get_columns(_TABLE) if c["name"] in _COSTS}


def _require_offline_rebuild(bind):
    if bind.exec_driver_sql("PRAGMA foreign_keys").scalar():
        raise RuntimeError(
            "Exact execution-cost migration requires an offline SQLite connection with "
            "foreign_keys=OFF; check foreign_key_check before re-enabling enforcement"
        )


def upgrade() -> None:
    bind = op.get_bind()
    columns = {
        name: c for name, c in _columns(bind).items() if not isinstance(c["type"], sa.String)
    }
    if not columns:
        return
    _require_offline_rebuild(bind)
    old = sa.table(
        _TABLE,
        sa.column("id", sa.Integer),
        *(sa.column(name, sa.Numeric(18, 8)) for name in columns),
    )
    staging = sa.Table(
        "_zeroth_exact_execution_costs",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        *(sa.Column(name, sa.String(20)) for name in columns),
        prefixes=["TEMPORARY"],
    )
    staging.create(bind)
    try:
        # Use the prior mapper's conversion, not SQLite CAST/printf. They expose
        # different decimal assertions for large binary floats. Stream bounded
        # batches through connection-local staging; never infer the lost digits.
        for rows in bind.execute(sa.select(old)).mappings().partitions(1000):
            bind.execute(
                staging.insert(),
                [
                    {
                        "id": row["id"],
                        **{
                            name: format(row[name], "f") if row[name] is not None else None
                            for name in columns
                        },
                    }
                    for row in rows
                ],
            )
        with op.batch_alter_table(_TABLE, recreate="always") as batch:
            for name, column in columns.items():
                batch.alter_column(
                    name,
                    existing_type=column["type"],
                    type_=sa.String(20),
                    existing_nullable=column["nullable"],
                )
        updated = sa.table(
            _TABLE,
            sa.column("id", sa.Integer),
            *(sa.column(name, sa.String(20)) for name in columns),
        )
        bind.execute(
            updated.update().values(
                {
                    name: sa.select(staging.c[name])
                    .where(staging.c.id == updated.c.id)
                    .scalar_subquery()
                    for name in columns
                }
            )
        )
    finally:
        staging.drop(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    columns = _columns(bind)
    if not columns:
        return
    table = sa.table(_TABLE, *(sa.column(name, sa.String(20)) for name in columns))
    if bind.execute(
        sa.select(sa.literal(1))
        .select_from(table)
        .where(sa.or_(*(table.c[name].is_not(None) for name in columns)))
        .limit(1)
    ).first():
        raise RuntimeError(
            "Cannot discard exact execution amounts; use a compatible reader rollback"
        )
    _require_offline_rebuild(bind)
    with op.batch_alter_table(_TABLE, recreate="always") as batch:
        for name, column in columns.items():
            batch.alter_column(
                name,
                existing_type=column["type"],
                type_=sa.Numeric(18, 8),
                existing_nullable=column["nullable"],
            )
