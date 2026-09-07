"""Additive namespace upgrade preserves historical rows and guards rollback."""

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import create_engine, inspect, text

from zeroth.econ.plane import database


def test_outcome_workflow_migration_preserves_data_and_refuses_lossy_downgrade(tmp_path):
    migration = importlib.import_module(
        "zeroth.econ.plane._migrations.versions.20260906_18_outcome_workflow_identity"
    )
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'migration.db'}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE outcome_events (id INTEGER PRIMARY KEY, tenant_id VARCHAR(128), "
                "capability_id VARCHAR(128), implementation_id VARCHAR(128), outcome_value VARCHAR(255))"
            )
        )
        conn.execute(
            text("INSERT INTO outcome_events VALUES (1, 'tenant-a', 'invoice', 'v1', 'True')")
        )
        migration.op = Operations(MigrationContext.configure(conn))
        migration.upgrade()
        migration.upgrade()
        assert conn.execute(text("SELECT * FROM outcome_events")).one() == (
            1,
            "tenant-a",
            "invoice",
            "v1",
            "True",
            None,
            None,
        )
        assert "ix_outcome_events_tenant_workflow_version" in {
            index["name"] for index in inspect(conn).get_indexes("outcome_events")
        }
        # Old-only data can downgrade without rewriting it.
        migration.downgrade()
        assert conn.execute(text("SELECT * FROM outcome_events")).one() == (
            1,
            "tenant-a",
            "invoice",
            "v1",
            "True",
        )
        migration.upgrade()
        conn.execute(text("UPDATE outcome_events SET workflow_id='invoice', workflow_version='v1'"))
        with pytest.raises(RuntimeError, match="workflow identity"):
            migration.downgrade()
        assert (
            conn.execute(text("SELECT workflow_id FROM outcome_events")).scalar_one() == "invoice"
        )
    assert {
        ("outcome_events", column, "20260906_18") for column in ("workflow_id", "workflow_version")
    } <= set(database._CHAIN_OWNED_COLUMNS)
