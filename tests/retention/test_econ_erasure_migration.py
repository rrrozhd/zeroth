from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from tests.conftest import requires_docker


def _econ_config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[2]
    config = Config()
    config.set_main_option("script_location", str(root / "src/zeroth/econ/plane/_migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _assert_receipt_table(database_url: str, *, expected: bool) -> None:
    engine = sa.create_engine(database_url)
    try:
        tables = set(sa.inspect(engine).get_table_names())
        assert ("econ_erasure_receipts" in tables) is expected
    finally:
        engine.dispose()


def test_econ_erasure_receipt_migration_roundtrips_sqlite(tmp_path: Path, monkeypatch) -> None:
    database_url = f"sqlite:///{tmp_path / 'econ-receipts.db'}"
    # setenv, not delenv: alembic's env.py prefers ECP_DATABASE_URL over the
    # config option, and with the var deleted a first-time econ_plane.config
    # import inside this test binds the settings singleton to ./econ_plane.db
    # at the repo root for the rest of the session.
    monkeypatch.setenv("ECP_DATABASE_URL", database_url)
    config = _econ_config(database_url)
    command.upgrade(config, "20260712_03")
    _assert_receipt_table(database_url, expected=True)
    command.downgrade(config, "20260226_02")
    _assert_receipt_table(database_url, expected=False)
    command.upgrade(config, "20260712_03")
    _assert_receipt_table(database_url, expected=True)


@requires_docker
def test_econ_erasure_receipt_migration_runs_on_postgres(postgres_container, monkeypatch) -> None:
    from sqlalchemy.engine import make_url

    root_url = make_url(postgres_container.get_connection_url().replace("psycopg2", "psycopg"))
    database_name = f"econ_receipts_{uuid4().hex[:10]}"
    admin_engine = sa.create_engine(root_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as connection:
        connection.execute(sa.text(f'CREATE DATABASE "{database_name}"'))
    database_url = root_url.set(database=database_name).render_as_string(hide_password=False)
    monkeypatch.setenv("ECP_DATABASE_URL", database_url)
    try:
        command.upgrade(_econ_config(database_url), "20260712_03")
        _assert_receipt_table(database_url, expected=True)
    finally:
        admin_engine.dispose()
        cleanup_engine = sa.create_engine(
            root_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
        )
        with cleanup_engine.connect() as connection:
            connection.execute(
                sa.text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :database_name"
                ),
                {"database_name": database_name},
            )
            connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        cleanup_engine.dispose()
