"""Source window schema upgrades preserve old evidence and refuse lossy rollback."""

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import inspect, text

from tests.econ_plane.test_sdk_evidence_namespace import engine as database_engine
from zeroth.econ.plane.database import _missing_chain_owned_columns


engine = database_engine


def test_source_window_migration_preserves_legacy_and_guards_rollback(engine):
    migration = importlib.import_module(
        "zeroth.econ.plane._migrations.versions.20260906_19_source_window"
    )
    with engine.begin() as conn:
        migration.op = Operations(MigrationContext.configure(conn))
        migration.downgrade()
        assert _missing_chain_owned_columns(conn) == (
            ("execution_events", "source_window_id", "20260906_19"),
        )
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
            text("SELECT execution_id, source_window_id FROM execution_events")
        ).one() == (
            "legacy",
            None,
        )
        assert "ix_execution_events_tenant_source_window" in {
            index["name"] for index in inspect(conn).get_indexes("execution_events")
        }
        migration.downgrade()
        migration.upgrade()
        conn.execute(text("UPDATE execution_events SET source_window_id='batch'"))
        with pytest.raises(RuntimeError, match="source window identity"):
            migration.downgrade()
        assert (
            conn.execute(text("SELECT source_window_id FROM execution_events")).scalar_one()
            == "batch"
        )
