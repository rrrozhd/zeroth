from __future__ import annotations

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect


def test_economic_loop_migration_adds_schedules_rollouts_and_calibration() -> None:
    migration = importlib.import_module(
        "zeroth.econ.plane._migrations.versions.20260902_19_economic_loop"
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE probabilistic_migration_decisions (decision_id VARCHAR(40) PRIMARY KEY)"
        )
        context = MigrationContext.configure(connection)
        migration.op = Operations(context)
        migration.upgrade()
        inspector = inspect(connection)

        assert {
            "probabilistic_decision_schedules",
            "randomized_rollouts",
            "randomized_rollout_assignments",
            "randomized_rollout_verifications",
            "forecast_calibration_observations",
        }.issubset(inspector.get_table_names())
        assert "evidence_lineage_json" in {
            column["name"] for column in inspector.get_columns("probabilistic_migration_decisions")
        }
