from __future__ import annotations

import importlib
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect


def test_backtest_revision_creates_and_drops_immutable_history(tmp_path: Path) -> None:
    migration = importlib.import_module(
        "zeroth.econ.plane._migrations.versions.20260831_15_economic_backtests"
    )
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'migration.db'}")
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

        inspector = inspect(connection)
        assert "economic_backtests" in inspector.get_table_names()
        assert {column["name"] for column in inspector.get_columns("economic_backtests")} == {
            "backtest_id",
            "tenant_id",
            "request_digest",
            "workflow",
            "baseline_version",
            "node_id",
            "incumbent_model",
            "candidate_model",
            "verdict",
            "provider_call_credits",
            "report_json",
            "period_start",
            "evaluated_at",
            "evaluated_by",
        }
        indexes = {index["name"]: index for index in inspector.get_indexes("economic_backtests")}
        assert indexes["uq_economic_backtests_tenant_digest"]["unique"] == 1

        migration.downgrade()
        assert "economic_backtests" not in inspect(connection).get_table_names()
