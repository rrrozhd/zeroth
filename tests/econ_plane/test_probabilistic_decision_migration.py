from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect


def test_probabilistic_decision_revision_adds_and_rolls_back_snapshot_history(
    tmp_path: Path,
) -> None:
    baseline = importlib.import_module(
        "zeroth.econ.plane._migrations.versions.20260901_17_cloud_activation"
    )
    try:
        migration = importlib.import_module(
            "zeroth.econ.plane._migrations.versions.20260901_18_probabilistic_decisions"
        )
    except ModuleNotFoundError:
        pytest.fail("probabilistic decision migration is not implemented")

    assert migration.down_revision == "20260901_17"
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'migration.db'}")
    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        baseline.op = operations
        migration.op = operations
        baseline.upgrade()
        migration.upgrade()

        inspector = inspect(connection)
        assert "probabilistic_migration_decisions" in inspector.get_table_names()
        assert {
            "decision_id",
            "tenant_id",
            "request_digest",
            "workload",
            "incumbent_model",
            "candidate_model",
            "verdict",
            "recommended_action",
            "evidence_json",
            "policy_json",
            "report_json",
            "evaluated_at",
            "evaluated_by",
        } == {
            column["name"] for column in inspector.get_columns("probabilistic_migration_decisions")
        }

        migration.downgrade()
        assert "probabilistic_migration_decisions" not in inspect(connection).get_table_names()
        assert "cloud_tenant_bindings" in inspect(connection).get_table_names()
