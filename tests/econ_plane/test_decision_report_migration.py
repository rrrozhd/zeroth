from __future__ import annotations

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect


def test_decision_report_migration_adds_artifact_and_delivery_tables() -> None:
    migration = importlib.import_module(
        "zeroth.econ.plane._migrations.versions.20260902_20_decision_reports"
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        migration.op = Operations(context)
        migration.upgrade()
        inspector = inspect(connection)

        assert {"decision_reports", "decision_report_deliveries"}.issubset(
            inspector.get_table_names()
        )
        assert "pdf_bytes" in {
            column["name"] for column in inspector.get_columns("decision_reports")
        }
