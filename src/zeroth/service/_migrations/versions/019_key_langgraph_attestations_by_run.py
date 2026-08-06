"""Key LangGraph run attestations by signed governance run identity."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "019"
down_revision = "018"
branch_labels = None
depends_on = None

_TABLE = "langgraph_run_attestations"
_TEMP = "langgraph_run_attestations_v019"


def _columns(*, include_run_id: bool) -> list[sa.Column[Any]]:
    columns = [
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("deployment_ref", sa.String(), nullable=False),
    ]
    if include_run_id:
        columns.append(sa.Column("run_id", sa.String(), nullable=False))
    columns.extend(
        [
            sa.Column("correlation_id", sa.String(), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=False),
            sa.Column("signature", sa.Text(), nullable=False),
            sa.Column("signing_key_id", sa.String(), nullable=False),
            sa.Column("algorithm", sa.String(), nullable=False),
        ]
    )
    return columns


def _rows() -> list[Mapping[str, Any]]:
    connection = op.get_bind()
    return list(
        connection.execute(
            sa.text(f"SELECT * FROM {_TABLE} ORDER BY tenant_id, deployment_ref, correlation_id")
        ).mappings()
    )


def _backfilled_run_id(row: Mapping[str, Any]) -> str:
    payload = json.loads(row["payload_json"])
    run_id = payload.get("run_id", row["correlation_id"])
    if not isinstance(run_id, str) or not run_id:
        raise RuntimeError("LangGraph attestation has an invalid signed run identity")
    return run_id


def upgrade() -> None:
    rows = [dict(row) | {"run_id": _backfilled_run_id(row)} for row in _rows()]
    identities = {(row["tenant_id"], row["deployment_ref"], row["run_id"]) for row in rows}
    if len(identities) != len(rows):
        raise RuntimeError("LangGraph attestation signed run identity collisions")

    table = op.create_table(
        _TEMP,
        *_columns(include_run_id=True),
        sa.PrimaryKeyConstraint("tenant_id", "deployment_ref", "run_id"),
    )
    if rows:
        op.bulk_insert(table, rows)
    op.drop_table(_TABLE)
    op.rename_table(_TEMP, _TABLE)


def downgrade() -> None:
    rows = _rows()
    correlations = {
        (row["tenant_id"], row["deployment_ref"], row["correlation_id"]) for row in rows
    }
    if len(correlations) != len(rows):
        raise RuntimeError("LangGraph attestation correlation collisions block downgrade")

    table = op.create_table(
        _TEMP,
        *_columns(include_run_id=False),
        sa.PrimaryKeyConstraint("tenant_id", "deployment_ref", "correlation_id"),
    )
    if rows:
        op.bulk_insert(
            table,
            [{key: value for key, value in row.items() if key != "run_id"} for row in rows],
        )
    op.drop_table(_TABLE)
    op.rename_table(_TEMP, _TABLE)
