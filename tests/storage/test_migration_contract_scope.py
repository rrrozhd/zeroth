"""Migration 023 gives contract versions tenant-local identity."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, text


def _config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "src/zeroth/service/_migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _insert_legacy(connection, name: str = "customer", version: int = 1) -> None:
    connection.execute(
        text(
            """INSERT INTO contract_versions (
                contract_name, version, model_path, schema_json, metadata_json, created_at
            ) VALUES (:name, :version, 'models:Customer', '{}', '{}', '2026-08-11')"""
        ),
        {"name": name, "version": version},
    )


def test_upgrade_preserves_rows_and_installs_tenant_local_identity(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'contract-scope.db'}"
    config = _config(database_url)
    command.upgrade(config, "022")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        _insert_legacy(connection)
    engine.dispose()

    command.upgrade(config, "023")
    engine = create_engine(database_url)
    try:
        assert inspect(engine).get_pk_constraint("contract_versions")["constrained_columns"] == [
            "tenant_id",
            "contract_name",
            "version",
        ]
        columns = {
            column["name"]: column for column in inspect(engine).get_columns("contract_versions")
        }
        assert columns["tenant_id"]["nullable"] is False
        assert columns["tenant_id"]["default"] is None
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT tenant_id, contract_name, version FROM contract_versions")
            ).one() == ("default", "customer", 1)
    finally:
        engine.dispose()


def test_fresh_head_has_scoped_contract_identity(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'fresh-contract-scope.db'}"
    command.upgrade(_config(database_url), "head")
    engine = create_engine(database_url)
    try:
        assert inspect(engine).get_pk_constraint("contract_versions")["constrained_columns"] == [
            "tenant_id",
            "contract_name",
            "version",
        ]
    finally:
        engine.dispose()


def test_downgrade_collision_fails_before_ddl_and_is_retryable(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'contract-downgrade.db'}"
    config = _config(database_url)
    command.upgrade(config, "023")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        for tenant in ("tenant-a", "tenant-b"):
            connection.execute(
                text(
                    """INSERT INTO contract_versions (
                        tenant_id, contract_name, version, model_path,
                        schema_json, metadata_json, created_at
                    ) VALUES (:tenant, 'shared', 1, 'models:Customer', '{}', '{}', '2026-08-11')"""
                ),
                {"tenant": tenant},
            )
    engine.dispose()

    with pytest.raises(RuntimeError, match="duplicate global identity"):
        command.downgrade(config, "022")

    engine = create_engine(database_url)
    assert inspect(engine).get_pk_constraint("contract_versions")["constrained_columns"] == [
        "tenant_id",
        "contract_name",
        "version",
    ]
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM contract_versions WHERE tenant_id='tenant-b'"))
    engine.dispose()

    command.downgrade(config, "022")
    engine = create_engine(database_url)
    try:
        assert inspect(engine).get_pk_constraint("contract_versions")["constrained_columns"] == [
            "contract_name",
            "version",
        ]
    finally:
        engine.dispose()
