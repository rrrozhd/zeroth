from __future__ import annotations

import importlib
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect


def test_billing_sync_revision_adds_and_rolls_back_projection_metadata(tmp_path: Path) -> None:
    baseline = importlib.import_module(
        "zeroth.econ.plane._migrations.versions.20260831_14_economic_decisions"
    )
    migration = importlib.import_module(
        "zeroth.econ.plane._migrations.versions.20260901_16_billing_sync"
    )
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'migration.db'}")
    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        baseline.op = operations
        migration.op = operations
        baseline.upgrade()
        migration.upgrade()

        inspector = inspect(connection)
        assert "billing_event_receipts" in inspector.get_table_names()
        assert {column["name"] for column in inspector.get_columns("billing_event_receipts")} == {
            "tenant_id",
            "provider",
            "event_id",
            "external_subscription_id",
            "payload_digest",
            "disposition",
            "occurred_at",
            "processed_at",
        }
        subscription_columns = {
            column["name"] for column in inspector.get_columns("cloud_subscriptions")
        }
        assert {
            "billing_provider",
            "external_price_id",
            "last_billing_event_id",
            "last_billing_event_at",
        } <= subscription_columns

        migration.downgrade()
        inspector = inspect(connection)
        assert "billing_event_receipts" not in inspector.get_table_names()
        rolled_back = {column["name"] for column in inspector.get_columns("cloud_subscriptions")}
        assert "billing_provider" not in rolled_back
        assert "last_billing_event_at" not in rolled_back
