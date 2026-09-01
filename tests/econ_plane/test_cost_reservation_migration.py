from __future__ import annotations

import importlib
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def test_cost_reservation_migration_declares_atomic_identity_and_ledger_fields() -> None:
    migration = importlib.import_module(
        "zeroth.econ.plane._migrations.versions.20260822_08_cost_reservations"
    )
    plan = migration.migration_plan("postgresql")
    assert plan == migration.migration_plan("sqlite")
    assert plan["table"] == "cost_reservations"
    assert plan["unique_identity"] == ("tenant_id", "operation_id")
    assert {
        "campaign_id",
        "run_id",
        "max_cost_usd",
        "held_cost_usd",
        "actual_cost_usd",
        "released_cost_usd",
        "cost_measurement",
        "cost_event_id",
        "provider_request_id",
        "cleanup_status",
    } <= set(plan["fields"])


def test_econ_alembic_head_creates_reservations_and_event_evidence_fields(
    tmp_path, monkeypatch
) -> None:
    database = tmp_path / "econ.db"
    url = f"sqlite+pysqlite:///{database}"
    monkeypatch.setenv("ECP_DATABASE_URL", url)
    config = Config()
    root = Path(__file__).parents[2]
    config.set_main_option("script_location", str(root / "src/zeroth/econ/plane/_migrations"))
    config.set_main_option("sqlalchemy.url", url)

    # Model a live database already at the prior head. Earlier econ revisions
    # alter existing application tables rather than creating the full ORM schema.
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        connection.execute(text("INSERT INTO alembic_version (version_num) VALUES ('20260812_07')"))
        connection.execute(
            text(
                "CREATE TABLE execution_events ("
                "id INTEGER PRIMARY KEY, tenant_id VARCHAR(128), execution_id VARCHAR(128))"
            )
        )
    engine.dispose()

    command.upgrade(config, "head")

    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        assert "cost_reservations" in inspector.get_table_names()
        assert {
            "campaign_id",
            "operation_id",
            "deployment_ref",
            "evidence_kind",
            "provider_request_id",
            "cleanup_status",
        } <= {column["name"] for column in inspector.get_columns("execution_events")}
        assert {"deployment_ref", "evidence_kind"} <= {
            column["name"] for column in inspector.get_columns("cost_reservations")
        }
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
                ).scalar_one() == ("20260901_17")
    finally:
        engine.dispose()


def test_cost_evidence_migration_classifies_known_control_proofs(tmp_path, monkeypatch) -> None:
    database = tmp_path / "econ.db"
    url = f"sqlite+pysqlite:///{database}"
    monkeypatch.setenv("ECP_DATABASE_URL", url)
    config = Config()
    root = Path(__file__).parents[2]
    config.set_main_option("script_location", str(root / "src/zeroth/econ/plane/_migrations"))
    config.set_main_option("sqlalchemy.url", url)

    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260822_08')"))
        connection.execute(
            text(
                "CREATE TABLE execution_events (id INTEGER PRIMARY KEY, tenant_id VARCHAR(128), "
                "execution_id VARCHAR(128), operation_id VARCHAR(192))"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE cost_reservations (id INTEGER PRIMARY KEY, tenant_id VARCHAR(128), "
                "operation_id VARCHAR(192))"
            )
        )
        connection.execute(
            text(
                "INSERT INTO cost_reservations (id, tenant_id, operation_id) "
                "VALUES (1, 'tenant-a', 'control-gate:campaign:concurrent-a'), "
                "(2, 'tenant-a', 'workflow:run:agent')"
            )
        )
    engine.dispose()

    command.upgrade(config, "head")

    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text("SELECT operation_id, evidence_kind FROM cost_reservations ORDER BY id")
            ).all()
            assert rows == [
                ("control-gate:campaign:concurrent-a", "synthetic_control"),
                ("workflow:run:agent", "legacy_unknown"),
            ]
    finally:
        engine.dispose()
