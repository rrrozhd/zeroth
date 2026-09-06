"""Ownership upgrade preserves legacy records and installs real uniqueness."""

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from tests.econ_plane.test_sdk_evidence_namespace import engine as database_engine
from zeroth.econ.plane.database import _missing_chain_owned_columns
from zeroth.econ.plane.instrumentation.models import ExecutionEvent

engine = database_engine


def test_charge_migration_keeps_legacy_unknown_and_refuses_lossy_rollback(engine):
    migration = importlib.import_module(
        "zeroth.econ.plane._migrations.versions.20260906_20_charge_ownership"
    )
    with engine.begin() as conn:
        migration.op = Operations(MigrationContext.configure(conn))
        migration.downgrade()
        assert {item[1] for item in _missing_chain_owned_columns(conn)} == {
            "cost_role",
            "charge_id",
        }
        conn.execute(
            text(
                "INSERT INTO execution_events (tenant_id, execution_id, join_key, timestamp, "
                "capability_id, implementation_id, model_version, evidence_kind, dimensions, "
                "cost_measurement, usage_measurement, latency_ms, compute_time_ms, attempt, metadata) VALUES "
                "('tenant-a', 'legacy', 'run', '2026-09-06', 'invoice', 'v1', 'model', "
                "'legacy_unknown', '{}', 'unmeasured', 'unmeasured', 0, 0, 1, '{}')"
            )
        )
        migration.upgrade()
        migration.upgrade()
        assert _missing_chain_owned_columns(conn) == ()
        assert conn.execute(
            text("SELECT execution_id, cost_role, charge_id FROM execution_events")
        ).one() == (
            "legacy",
            None,
            None,
        )
        index = next(
            i
            for i in inspect(conn).get_indexes("execution_events")
            if i["name"] == "uq_execution_events_tenant_charge_id"
        )
        assert index["unique"]
        assert index["column_names"] == ["tenant_id", "charge_id"]
        migration.downgrade()
        migration.upgrade()
        conn.execute(text("UPDATE execution_events SET cost_role='charge', charge_id='charge'"))
        with pytest.raises(RuntimeError, match="charge ownership"):
            migration.downgrade()
    # Exercise the actual index without ingestion's pre-check.
    with engine.begin() as conn:
        row = dict(conn.execute(ExecutionEvent.__table__.select()).mappings().one())
        row.pop("id")
        row["execution_id"] = "another"
        with pytest.raises(IntegrityError):
            with conn.begin_nested():
                conn.execute(ExecutionEvent.__table__.insert(), row)
