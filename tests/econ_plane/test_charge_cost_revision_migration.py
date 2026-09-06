"""Migration preserves charge owners and refuses to discard revision history."""

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from tests.econ_plane.test_charge_cost_revisions import engine as database_engine, seed, revision
from tests.econ_plane.test_sdk_evidence_namespace import scoped
from zeroth.econ.charge_costs import ChargeCostRevision
from zeroth.econ.plane.instrumentation.charge_costs import ingest_revision
from zeroth.econ.plane.instrumentation.models import ExecutionEvent

engine = database_engine


def test_revision_migration_keeps_owners_and_refuses_lossy_downgrade(engine):
    migration = importlib.import_module("zeroth.econ.plane._migrations.versions.20260906_22_charge_cost_revisions")
    with Session(engine) as raw:
        seed(scoped(raw))
        before = [(row.id, row.execution_id, row.charge_id, row.token_cost_usd) for row in raw.scalars(select(ExecutionEvent))]
    with engine.begin() as conn:
        migration.op = Operations(MigrationContext.configure(conn))
        migration.downgrade()
        assert "charge_cost_revisions" not in inspect(conn).get_table_names()
        migration.upgrade()
        migration.upgrade()
        columns = {col["name"]: col for col in inspect(conn).get_columns("charge_cost_revisions")}
        if engine.dialect.name == "sqlite":
            assert str(columns["token_cost_usd"]["type"]) == "VARCHAR(20)"
        else:
            assert str(columns["token_cost_usd"]["type"]) == "NUMERIC(18, 8)"
    with Session(engine) as raw:
        assert [(row.id, row.execution_id, row.charge_id, row.token_cost_usd) for row in raw.scalars(select(ExecutionEvent))] == before
        ingest_revision(scoped(raw), ChargeCostRevision.model_validate(revision("9999999999.12345678")))
    with engine.begin() as conn:
        migration.op = Operations(MigrationContext.configure(conn))
        with pytest.raises(RuntimeError, match="Cannot discard charge cost revisions"):
            migration.downgrade()
