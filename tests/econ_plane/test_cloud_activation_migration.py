from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect


def test_activation_revision_adds_and_rolls_back_identity_bindings(tmp_path: Path) -> None:
    baseline = importlib.import_module(
        "zeroth.econ.plane._migrations.versions.20260831_14_economic_decisions"
    )
    billing = importlib.import_module(
        "zeroth.econ.plane._migrations.versions.20260901_16_billing_sync"
    )
    try:
        migration = importlib.import_module(
            "zeroth.econ.plane._migrations.versions.20260901_17_cloud_activation"
        )
    except ModuleNotFoundError:
        pytest.fail("cloud activation migration is not implemented")

    assert migration.down_revision == "20260901_16"
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'migration.db'}")
    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        baseline.op = operations
        billing.op = operations
        migration.op = operations
        baseline.upgrade()
        billing.upgrade()
        migration.upgrade()

        inspector = inspect(connection)
        assert {
            "cloud_tenant_bindings",
            "cloud_identity_memberships",
        } <= set(inspector.get_table_names())
        assert {
            "local_tenant_id",
            "provider",
            "external_organization_id",
            "created_at",
        } == {
            column["name"]
            for column in inspector.get_columns("cloud_tenant_bindings")
        }
        assert {
            "tenant_id",
            "provider",
            "external_user_id",
            "external_organization_id",
            "email",
            "created_at",
            "updated_at",
        } == {
            column["name"]
            for column in inspector.get_columns("cloud_identity_memberships")
        }

        migration.downgrade()
        inspector = inspect(connection)
        assert "cloud_tenant_bindings" not in inspector.get_table_names()
        assert "cloud_identity_memberships" not in inspector.get_table_names()
        assert "cloud_subscriptions" in inspector.get_table_names()
