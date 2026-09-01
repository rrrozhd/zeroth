"""Add the first-class economic-debugger identity spine."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260830_11"
down_revision = "20260824_10"
branch_labels = None
depends_on = None

_TABLE = "execution_events"
_COLUMNS = (
    sa.Column("workflow_id", sa.String(length=192), nullable=True),
    sa.Column("workflow_version", sa.String(length=192), nullable=True),
    sa.Column("run_id", sa.String(length=128), nullable=True),
    sa.Column("step_id", sa.String(length=192), nullable=True),
    sa.Column("attempt", sa.Integer(), nullable=False, server_default=sa.text("1")),
    sa.Column("subject_id", sa.String(length=192), nullable=True),
    sa.Column("dimensions", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
)
_INDEXES = (
    (
        "ix_execution_events_tenant_workflow_time",
        ["tenant_id", "workflow_id", "timestamp"],
    ),
    ("ix_execution_events_tenant_subject", ["tenant_id", "subject_id"]),
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in inspector.get_table_names():
        return
    present = {column["name"] for column in inspector.get_columns(_TABLE)}
    with op.batch_alter_table(_TABLE) as batch:
        for column in _COLUMNS:
            if column.name not in present:
                batch.add_column(column)

    if "capability_id" in present:
        op.execute(
            "UPDATE execution_events SET "
            "workflow_id = capability_id WHERE workflow_id IS NULL"
        )
    if "implementation_id" in present:
        op.execute(
            "UPDATE execution_events SET "
            "workflow_version = implementation_id WHERE workflow_version IS NULL"
        )
    if "join_key" in present:
        op.execute(
            "UPDATE execution_events SET run_id = join_key WHERE run_id IS NULL"
        )
    op.execute("UPDATE execution_events SET attempt = 1 WHERE attempt IS NULL")
    op.execute("UPDATE execution_events SET dimensions = '{}' WHERE dimensions IS NULL")

    inspector = sa.inspect(op.get_bind())
    present_indexes = {index["name"] for index in inspector.get_indexes(_TABLE)}
    present_after = {column["name"] for column in inspector.get_columns(_TABLE)}
    for name, columns in _INDEXES:
        if name not in present_indexes and set(columns) <= present_after:
            op.create_index(name, _TABLE, columns, unique=False)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in inspector.get_table_names():
        return
    present_indexes = {index["name"] for index in inspector.get_indexes(_TABLE)}
    for name, _columns in reversed(_INDEXES):
        if name in present_indexes:
            op.drop_index(name, table_name=_TABLE)
    present = {column["name"] for column in inspector.get_columns(_TABLE)}
    with op.batch_alter_table(_TABLE) as batch:
        for column in reversed(_COLUMNS):
            if column.name in present:
                batch.drop_column(column.name)
