from __future__ import annotations

import importlib
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect


def test_economic_decision_revision_creates_and_drops_retained_history(tmp_path: Path) -> None:
    migration = importlib.import_module(
        "zeroth.econ.plane._migrations.versions.20260831_14_economic_decisions"
    )
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'migration.db'}")
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

        inspector = inspect(connection)
        assert "economic_decisions" in inspector.get_table_names()
        assert "decision_schedules" in inspector.get_table_names()
        assert "cloud_api_keys" in inspector.get_table_names()
        assert "cloud_subscriptions" in inspector.get_table_names()
        assert "cloud_usage_counters" in inspector.get_table_names()
        assert {column["name"] for column in inspector.get_columns("economic_decisions")} == {
            "decision_id",
            "tenant_id",
            "evidence_digest",
            "workflow",
            "baseline_version",
            "candidate_version",
            "outcome_type",
            "verdict",
            "recommended_action",
            "report_json",
            "evaluated_at",
            "evaluated_by",
        }
        indexes = {index["name"]: index for index in inspector.get_indexes("economic_decisions")}
        assert indexes["uq_economic_decisions_tenant_digest"]["unique"] == 1

        migration.downgrade()
        assert "economic_decisions" not in inspect(connection).get_table_names()
        assert "decision_schedules" not in inspect(connection).get_table_names()
        assert "cloud_api_keys" not in inspect(connection).get_table_names()
        assert "cloud_subscriptions" not in inspect(connection).get_table_names()
        assert "cloud_usage_counters" not in inspect(connection).get_table_names()
