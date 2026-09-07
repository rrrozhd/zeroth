"""Exact storage migration preserves the assertions exposed by the old mapper."""

import importlib
from datetime import timedelta
from decimal import Decimal

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from tests.econ_plane.test_sdk_evidence_namespace import (
    NOW,
    engine as database_engine,
    execution,
    scoped,
    user,
)
from zeroth.econ.charge_costs import ChargeCostRevision
from zeroth.econ.plane.cloud.api import record_execution
from zeroth.econ.plane.instrumentation.charge_costs import ingest_revision
from zeroth.econ.plane.instrumentation.models import ChargeCostRevisionRecord, ExecutionEvent

engine = database_engine
FIELDS = ("token_cost_usd", "tool_cost_usd", "compute_cost_usd")


def migration(conn):
    module = importlib.import_module(
        "zeroth.econ.plane._migrations.versions.20260906_23_exact_execution_costs"
    )
    module.op = Operations(MigrationContext.configure(conn))
    return module


def old_reader():
    return sa.table(
        "execution_events",
        sa.column("id", sa.Integer),
        *(sa.column(name, sa.Numeric(18, 8)) for name in FIELDS),
    )


def seed_old(engine, extra_rows=0):
    with engine.begin() as conn:
        migration(conn).downgrade()  # Empty source table, equivalent prior storage.
    with Session(engine) as raw:
        db = scoped(raw)
        record_execution(
            execution(event_id="owned", cost_role="charge", charge_id="owned", cost_usd="0"),
            db,
            user(),
        )
        ingest_revision(
            db,
            ChargeCostRevision(
                charge_id="owned",
                asserted_at=NOW + timedelta(minutes=1),
                token_cost_usd="0.25",
                cost_measurement="measured",
                reason="preserve child assertion",
            ),
        )
        record_execution(execution(event_id="legacy", run="legacy", cost_usd=None), db, user())
        record_execution(execution(event_id="unknown", run="unknown", cost_usd=None), db, user())
    with Session(engine) as raw:
        raw.add_all(
            ExecutionEvent(
                tenant_id="tenant-a",
                execution_id=f"batch-{i}",
                timestamp=NOW,
                capability_id="invoice",
                implementation_id="v1",
                model_version="model",
                token_cost_usd=Decimal(i) / 100000000,
                cost_measurement="measured",
            )
            for i in range(extra_rows)
        )
        raw.commit()
    old = old_reader()
    with engine.begin() as conn:
        conn.execute(
            old.update()
            .where(old.c.id == 1)
            .values(
                token_cost_usd=Decimal("9999999999.12345678"),
                tool_cost_usd=Decimal("0.00000001"),
                compute_cost_usd=Decimal("0.12345678"),
            )
        )
        conn.execute(
            old.update().where(old.c.id == 2).values(token_cost_usd=Decimal("-0.12345678"))
        )
        amounts = list(conn.execute(sa.select(old).order_by(old.c.id)))
        identities = list(
            conn.execute(
                sa.text(
                    "SELECT id, tenant_id, execution_id, charge_id, metadata FROM execution_events ORDER BY id"
                )
            )
        )
    return amounts, identities


def test_upgrade_preserves_old_assertions_children_and_indexes(engine):
    before, identities = seed_old(engine, extra_rows=1001)
    assert len(before) == 1004  # Cross the migration's staging-batch boundary.
    if engine.dialect.name == "sqlite":
        assert before[0][1] == Decimal("9999999999.12345695")
        assert before[0][1] != Decimal("9999999999.12345678")  # Lost digits cannot be recovered.
    with engine.begin() as conn:
        migration(conn).upgrade()
        migration(conn).upgrade()
        after_identities = list(
            conn.execute(
                sa.text(
                    "SELECT id, tenant_id, execution_id, charge_id, metadata FROM execution_events ORDER BY id"
                )
            )
        )
        assert after_identities == identities
        columns = {c["name"]: c["type"] for c in sa.inspect(conn).get_columns("execution_events")}
        for name in FIELDS:
            assert str(columns[name]) == (
                "VARCHAR(20)" if engine.dialect.name == "sqlite" else "NUMERIC(18, 8)"
            )
        if engine.dialect.name == "sqlite":
            assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            assert "_zeroth_exact_execution_costs" not in sa.inspect(conn).get_temp_table_names()
        index_names = {index["name"] for index in sa.inspect(conn).get_indexes("execution_events")}
        assert "uq_execution_events_tenant_charge_id" in index_names
    with Session(engine) as raw:
        after = [
            (row.id, *(getattr(row, name) for name in FIELDS))
            for row in raw.scalars(sa.select(ExecutionEvent).order_by(ExecutionEvent.id))
        ]
        assert after == before
        child = raw.scalars(sa.select(ChargeCostRevisionRecord)).one()
        assert child.charge_id == "owned" and child.token_cost_usd == Decimal(".25")
    if engine.dialect.name == "sqlite":
        with engine.begin() as conn, pytest.raises(RuntimeError, match="Cannot discard exact"):
            migration(conn).downgrade()


def test_foreign_key_enforcement_refuses_rebuild_before_mutation(engine):
    if engine.dialect.name != "sqlite":
        # PostgreSQL requires neither a rebuild nor an enforcement override.
        with engine.begin() as conn:
            migration(conn).upgrade()
        return
    before, _ = seed_old(engine)
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        with pytest.raises(RuntimeError, match="offline SQLite"):
            migration(conn).upgrade()
        assert list(conn.execute(sa.select(old_reader()).order_by(sa.column("id")))) == before
        assert conn.scalar(sa.text("SELECT COUNT(*) FROM charge_cost_revisions")) == 1
        assert "_zeroth_exact_execution_costs" not in sa.inspect(conn).get_temp_table_names()
