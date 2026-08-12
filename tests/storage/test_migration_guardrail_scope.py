"""Migration 026 binds durable guardrail rows to tenant identity."""

from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, text

from tests.conftest import requires_docker


def _config(path: Path) -> Config:
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "src/zeroth/service/_migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _url_config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "src/zeroth/service/_migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_026_fresh_schema_has_composite_guardrail_identities(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    config = _config(tmp_path / "fresh.db")
    command.upgrade(config, "head")

    inspector = inspect(engine)
    assert inspector.get_pk_constraint("rate_limit_buckets")["constrained_columns"] == [
        "tenant_id",
        "bucket_key",
    ]
    assert inspector.get_pk_constraint("quota_counters")["constrained_columns"] == [
        "tenant_id",
        "counter_key",
    ]


def test_026_upgrade_backfills_default_and_preserves_guardrail_values(tmp_path: Path) -> None:
    path = tmp_path / "upgrade.db"
    config = _config(path)
    command.upgrade(config, "025")
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO rate_limit_buckets VALUES ('shared', 2.5, 'then', 7, 0.5)")
        )
        connection.execute(text("INSERT INTO quota_counters VALUES ('shared', 4, 'then', 99)"))

    command.upgrade(config, "026")

    with engine.connect() as connection:
        assert connection.execute(text("SELECT * FROM rate_limit_buckets")).mappings().one() == {
            "tenant_id": "default",
            "bucket_key": "shared",
            "token_count": 2.5,
            "last_refill_at": "then",
            "capacity": 7.0,
            "refill_rate": 0.5,
        }
        assert connection.execute(text("SELECT * FROM quota_counters")).mappings().one() == {
            "tenant_id": "default",
            "counter_key": "shared",
            "value": 4,
            "window_start": "then",
            "window_seconds": 99,
        }


def test_026_downgrade_refuses_non_default_tenant_rows_before_ddl(tmp_path: Path) -> None:
    path = tmp_path / "downgrade.db"
    config = _config(path)
    command.upgrade(config, "026")
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO quota_counters VALUES ('tenant-a', 'shared', 1, 'then', 99)")
        )

    with pytest.raises(RuntimeError, match="non-default tenant rows"):
        command.downgrade(config, "025")

    assert inspect(engine).get_pk_constraint("quota_counters")["constrained_columns"] == [
        "tenant_id",
        "counter_key",
    ]


@requires_docker
def test_026_postgres_upgrade_and_downgrade_avoid_constraint_name_collisions(
    postgres_container,
) -> None:
    database_url = postgres_container.get_connection_url().replace("psycopg2", "psycopg")
    config = _url_config(database_url)
    command.upgrade(config, "025")
    engine = create_engine(database_url)
    try:
        command.upgrade(config, "026")
        assert inspect(engine).get_pk_constraint("rate_limit_buckets")["constrained_columns"] == [
            "tenant_id",
            "bucket_key",
        ]
        command.downgrade(config, "025")
        assert inspect(engine).get_pk_constraint("rate_limit_buckets")["constrained_columns"] == [
            "bucket_key"
        ]
    finally:
        engine.dispose()
