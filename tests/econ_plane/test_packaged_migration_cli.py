"""The installed core wheel can migrate the hosted economic database."""

from pathlib import Path

from sqlalchemy import create_engine, inspect, text


def test_packaged_economic_migration_runner_reaches_cloud_head(tmp_path: Path) -> None:
    from zeroth.econ.plane.migrations import run_econ_migrations

    database_url = f"sqlite:///{tmp_path / 'hosted-econ.db'}"
    run_econ_migrations(database_url)

    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version_econ")
            ).scalar()
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert revision == "20260901_17"
    assert {
        "cloud_api_keys",
        "cloud_subscriptions",
        "cloud_tenant_bindings",
        "cloud_identity_memberships",
    } <= tables


def test_service_and_economic_migrations_share_one_database_without_colliding(
    tmp_path: Path,
) -> None:
    from zeroth.econ.plane.migrations import run_econ_migrations
    from zeroth.service.bootstrap.migrations import run_migrations

    database_url = f"sqlite:///{tmp_path / 'hosted-shared.db'}"
    run_migrations(database_url)

    engine = create_engine(database_url)
    try:
        service_tables = set(inspect(engine).get_table_names()) - {"alembic_version"}
    finally:
        engine.dispose()

    run_econ_migrations(database_url)

    engine = create_engine(database_url)
    try:
        tables = set(inspect(engine).get_table_names())
        with engine.connect() as connection:
            service_revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            econ_revision = connection.execute(
                text("SELECT version_num FROM alembic_version_econ")
            ).scalar_one()
    finally:
        engine.dispose()

    assert service_revision == "035"
    assert econ_revision == "20260901_17"
    assert service_tables <= tables
    assert {"alembic_version", "alembic_version_econ"} <= tables


def test_migrate_econ_cli_runs_only_the_economic_migration(monkeypatch, capsys) -> None:
    from zeroth.service import cli

    calls: list[str] = []
    monkeypatch.setattr(cli, "ensure_econ_schema", lambda: calls.append("econ"))
    monkeypatch.setattr(cli, "ensure_schema", lambda: calls.append("service"))

    assert cli.main(["migrate-econ"]) == 0
    assert calls == ["econ"]
    assert capsys.readouterr().out == "economic migrations applied\n"
