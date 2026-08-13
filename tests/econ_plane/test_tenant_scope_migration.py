from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from tests.conftest import requires_docker
from zeroth.econ.plane import database as database_module
from zeroth.econ.plane.capabilities import models as capability_models  # noqa: F401
from zeroth.econ.plane.connectors import models as connector_models  # noqa: F401
from zeroth.econ.plane.counterfactual import (
    models as counterfactual_models,  # noqa: F401
)
from zeroth.econ.plane.database import Base
from zeroth.econ.plane.instrumentation import (
    models as instrumentation_models,  # noqa: F401
)

_TABLES = (
    "execution_events",
    "outcome_events",
    "valuation_runs",
    "value_estimates",
    "connector_configs",
    "connector_outbox",
    "connector_delivery_log",
)


def _load_migration():
    path = (
        Path(__file__).parents[2]
        / "src/zeroth/econ/plane/_migrations/versions/20260811_05_tenant_scope.py"
    )
    spec = importlib.util.spec_from_file_location("tenant_scope_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _econ_config(database_url: str) -> Config:
    root = Path(__file__).parents[2]
    config = Config()
    config.set_main_option("script_location", str(root / "src/zeroth/econ/plane/_migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _legacy_schema(connection) -> None:
    connection.execute(
        text(
            "CREATE TABLE execution_events ("
            "id INTEGER PRIMARY KEY, "
            "tenant_id VARCHAR(128) DEFAULT 'tenant_default', "
            "execution_id VARCHAR(128) NOT NULL, payload VARCHAR(32), "
            "CONSTRAINT uq_execution_events_execution_id UNIQUE (execution_id))"
        )
    )
    connection.execute(
        text(
            "CREATE TABLE outcome_events ("
            "id INTEGER PRIMARY KEY, tenant_id VARCHAR(128) DEFAULT 'tenant_default', "
            "execution_id VARCHAR(128), payload VARCHAR(32))"
        )
    )
    connection.execute(
        text(
            "CREATE TABLE valuation_runs ("
            "id INTEGER PRIMARY KEY, tenant_id VARCHAR(128) DEFAULT 'tenant_default', "
            "payload VARCHAR(32))"
        )
    )
    connection.execute(
        text(
            "CREATE TABLE value_estimates ("
            "id INTEGER PRIMARY KEY, tenant_id VARCHAR(128) DEFAULT 'tenant_default', "
            "payload VARCHAR(32))"
        )
    )
    connection.execute(
        text(
            "CREATE TABLE connector_configs ("
            "id INTEGER PRIMARY KEY, tenant_id VARCHAR(128) DEFAULT 'tenant_default', "
            "payload VARCHAR(32))"
        )
    )
    connection.execute(
        text(
            "CREATE TABLE connector_outbox ("
            "id INTEGER PRIMARY KEY, tenant_id VARCHAR(128) DEFAULT 'tenant_default', "
            "payload VARCHAR(32))"
        )
    )
    connection.execute(
        text(
            "CREATE TABLE connector_delivery_log ("
            "id INTEGER PRIMARY KEY, outbox_id INTEGER NOT NULL, payload VARCHAR(32))"
        )
    )


def _scope_shape(bind, table: str) -> dict[str, object]:
    inspector = inspect(bind)
    columns = {column["name"]: column for column in inspector.get_columns(table)}
    return {
        "tenant_nullable": columns["tenant_id"]["nullable"],
        "tenant_default": columns["tenant_id"]["default"],
        "tenant_indexes": sorted(
            tuple(index["column_names"])
            for index in inspector.get_indexes(table)
            if "tenant_id" in index["column_names"]
        ),
        "tenant_uniques": sorted(
            tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints(table)
            if "tenant_id" in constraint["column_names"]
        ),
    }


def test_tenant_scope_upgrade_from_previous_sqlite_preserves_rows() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        _legacy_schema(connection)
        connection.execute(
            text(
                "INSERT INTO execution_events "
                "(id, tenant_id, execution_id, payload) VALUES "
                "(1, 'tenant_default', 'shared', 'event')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO outcome_events (id, tenant_id, execution_id, payload) "
                "VALUES (1, NULL, 'shared', 'outcome')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO connector_outbox (id, tenant_id, payload) "
                "VALUES (9, 'tenant-a', 'outbox')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO connector_delivery_log (id, outbox_id, payload) "
                "VALUES (11, 9, 'delivery')"
            )
        )
        for table in ("valuation_runs", "value_estimates", "connector_configs"):
            connection.execute(
                text(
                    f"INSERT INTO {table} (id, tenant_id, payload) "
                    "VALUES (1, 'tenant_default', 'preserved')"
                )
            )

        migration = _load_migration()
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

        assert connection.execute(
            text("SELECT tenant_id, execution_id, payload FROM execution_events")
        ).one() == ("default", "shared", "event")
        assert connection.execute(text("SELECT tenant_id, payload FROM outcome_events")).one() == (
            "default",
            "outcome",
        )
        assert connection.execute(
            text("SELECT tenant_id, outbox_id, payload FROM connector_delivery_log")
        ).one() == ("tenant-a", 9, "delivery")
        for table in ("valuation_runs", "value_estimates", "connector_configs"):
            assert connection.execute(text(f"SELECT tenant_id, payload FROM {table}")).one() == (
                "default",
                "preserved",
            )

        connection.execute(
            text(
                "INSERT INTO execution_events "
                "(id, tenant_id, execution_id, payload) "
                "VALUES (2, 'tenant-b', 'shared', 'other')"
            )
        )
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO execution_events "
                    "(id, tenant_id, execution_id, payload) "
                    "VALUES (3, 'tenant-b', 'shared', 'duplicate')"
                )
            )


def test_tenant_scope_downgrade_refuses_cross_tenant_execution_collisions_before_ddl() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        _legacy_schema(connection)
        connection.execute(
            text(
                "INSERT INTO execution_events "
                "(id, tenant_id, execution_id, payload) VALUES "
                "(1, 'tenant-a', 'collision', 'a')"
            )
        )
        migration = _load_migration()
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        connection.execute(
            text(
                "INSERT INTO execution_events "
                "(id, tenant_id, execution_id, payload) VALUES "
                "(2, 'tenant-b', 'collision', 'b')"
            )
        )
        shape_before = _scope_shape(connection, "execution_events")

        with pytest.raises(
            RuntimeError,
            match=r"1 cross-tenant execution_id collision.*collision.*2 tenants.*2 rows",
        ):
            migration.downgrade()

        assert _scope_shape(connection, "execution_events") == shape_before
        assert "tenant_id" in {
            column["name"] for column in inspect(connection).get_columns("connector_delivery_log")
        }
        assert connection.execute(
            text("SELECT tenant_id, execution_id FROM execution_events ORDER BY tenant_id")
        ).all() == [("tenant-a", "collision"), ("tenant-b", "collision")]


def test_tenant_scope_downgrade_round_trips_noncolliding_sqlite_data() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        _legacy_schema(connection)
        connection.execute(
            text(
                "INSERT INTO execution_events "
                "(id, tenant_id, execution_id, payload) VALUES "
                "(1, 'tenant-a', 'unique-a', 'a')"
            )
        )
        migration = _load_migration()
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

        migration.downgrade()

        execution_shape = _scope_shape(connection, "execution_events")
        assert execution_shape["tenant_default"] is not None
        assert execution_shape["tenant_uniques"] == []
        assert {
            tuple(constraint["column_names"])
            for constraint in inspect(connection).get_unique_constraints("execution_events")
        } == {("execution_id",)}
        assert "tenant_id" not in {
            column["name"] for column in inspect(connection).get_columns("connector_delivery_log")
        }
        assert connection.execute(
            text("SELECT tenant_id, execution_id, payload FROM execution_events")
        ).one() == ("tenant-a", "unique-a", "a")


def test_alembic_upgrade_from_previous_sqlite_revision_preserves_connector_rows(
    tmp_path: Path,
) -> None:
    root = Path(__file__).parents[2]
    database_path = tmp_path / "previous.db"
    url = f"sqlite+pysqlite:///{database_path}"
    config = Config()
    config.set_main_option("script_location", str(root / "src/zeroth/econ/plane/_migrations"))
    config.set_main_option("sqlalchemy.url", url)
    previous_url = os.environ.get("ECP_DATABASE_URL")
    os.environ["ECP_DATABASE_URL"] = url
    try:
        command.upgrade(config, "20260811_04")
        engine = create_engine(url)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO connector_outbox "
                    "(id, tenant_id, event_type, event_key, payload_json, status, attempts, "
                    "next_attempt_at, created_at) VALUES "
                    "(7, 'tenant_default', 'event', 'key', '{}', 'PENDING', 0, "
                    "'2026-08-11 00:00:00', '2026-08-11 00:00:00')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO connector_delivery_log "
                    "(id, outbox_id, connector_type, attempt, duration_ms, created_at) VALUES "
                    "(8, 7, 'posthog', 1, 0, '2026-08-11 00:00:00')"
                )
            )
        engine.dispose()

        command.upgrade(config, "head")

        engine = create_engine(url)
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT tenant_id, event_key FROM connector_outbox WHERE id = 7")
            ).one() == ("default", "key")
            assert connection.execute(
                text("SELECT tenant_id, outbox_id FROM connector_delivery_log WHERE id = 8")
            ).one() == ("default", 7)
            for table in ("connector_configs", "connector_outbox", "connector_delivery_log"):
                shape = _scope_shape(connection, table)
                assert shape["tenant_nullable"] is False
                assert shape["tenant_default"] is None
        engine.dispose()
    finally:
        if previous_url is None:
            os.environ.pop("ECP_DATABASE_URL", None)
        else:
            os.environ["ECP_DATABASE_URL"] = previous_url


@pytest.mark.postgres
@requires_docker
def test_tenant_scope_migration_executes_on_live_postgres(postgres_container, monkeypatch) -> None:
    root_url = make_url(postgres_container.get_connection_url().replace("psycopg2", "psycopg"))
    database_name = f"econ_tenant_scope_{uuid4().hex[:10]}"
    admin_engine = create_engine(root_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    database_url = root_url.set(database=database_name).render_as_string(hide_password=False)
    monkeypatch.setenv("ECP_DATABASE_URL", database_url)
    engine = None
    try:
        config = _econ_config(database_url)
        command.upgrade(config, "20260811_04")
        engine = create_engine(database_url)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE execution_events ("
                    "id INTEGER PRIMARY KEY, "
                    "tenant_id VARCHAR(128) DEFAULT 'tenant_default', "
                    "execution_id VARCHAR(128) NOT NULL, payload VARCHAR(32), "
                    # The cost columns are part of this table in every real
                    # database: nothing in the migration chain creates
                    # execution_events -- create_all does, from a model that has
                    # carried these three since the table was introduced. They
                    # are NOT NULL here because that is their state at
                    # 20260811_04; 20260812_04 is the revision that relaxes them.
                    "token_cost_usd NUMERIC(12, 4) NOT NULL, "
                    "tool_cost_usd NUMERIC(12, 4) NOT NULL, "
                    "compute_cost_usd NUMERIC(12, 4) NOT NULL, "
                    "CONSTRAINT uq_execution_events_execution_id UNIQUE (execution_id))"
                )
            )
            for table in ("outcome_events", "valuation_runs", "value_estimates"):
                connection.execute(
                    text(
                        f"CREATE TABLE {table} ("
                        "id INTEGER PRIMARY KEY, "
                        "tenant_id VARCHAR(128) DEFAULT 'tenant_default', "
                        "payload VARCHAR(32))"
                    )
                )
            connection.execute(
                text(
                    "INSERT INTO execution_events "
                    "(id, tenant_id, execution_id, payload, token_cost_usd, "
                    "tool_cost_usd, compute_cost_usd) VALUES "
                    "(1, 'tenant_default', 'shared', 'event', 0, 0, 0)"
                )
            )
            for table in ("outcome_events", "valuation_runs", "value_estimates"):
                connection.execute(
                    text(
                        f"INSERT INTO {table} (id, tenant_id, payload) "
                        "VALUES (1, 'tenant_default', 'preserved')"
                    )
                )
            connection.execute(
                text(
                    "INSERT INTO connector_configs "
                    "(id, tenant_id, connector_type, enabled, config_json, created_at, updated_at) "
                    "VALUES (3, 'tenant_default', 'posthog', FALSE, '{}', "
                    "'2026-08-11 00:00:00', '2026-08-11 00:00:00')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO connector_outbox "
                    "(id, tenant_id, event_type, event_key, payload_json, status, attempts, "
                    "next_attempt_at, created_at) VALUES "
                    "(7, 'tenant-a', 'event', 'key', '{}', 'PENDING', 0, "
                    "'2026-08-11 00:00:00', '2026-08-11 00:00:00')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO connector_delivery_log "
                    "(id, outbox_id, connector_type, attempt, duration_ms, created_at) VALUES "
                    "(8, 7, 'posthog', 1, 0, '2026-08-11 00:00:00')"
                )
            )
        engine.dispose()
        engine = None

        command.upgrade(config, "head")

        engine = create_engine(database_url)
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT tenant_id, execution_id, payload FROM execution_events")
            ).one() == ("default", "shared", "event")
            # 20260812_04 classifies a pre-existing row by its recorded costs.
            # An all-zero row is indistinguishable from one nobody measured, so
            # it must come out "unmeasured" rather than silently "measured".
            # Until this fixture carried the cost columns that revision could
            # not run here at all, and its classification had no live-Postgres
            # coverage anywhere.
            assert connection.execute(
                text("SELECT cost_measurement, usage_measurement FROM execution_events")
            ).one() == ("unmeasured", "unmeasured")
            for table in ("outcome_events", "valuation_runs", "value_estimates"):
                assert connection.execute(
                    text(f"SELECT tenant_id, payload FROM {table}")
                ).one() == ("default", "preserved")
            assert connection.execute(
                text("SELECT tenant_id, connector_type FROM connector_configs")
            ).one() == ("default", "posthog")
            assert connection.execute(
                text("SELECT tenant_id, outbox_id FROM connector_delivery_log WHERE id = 8")
            ).one() == ("tenant-a", 7)

            inspector = inspect(connection)
            for table in _TABLES:
                columns = {column["name"]: column for column in inspector.get_columns(table)}
                assert columns["tenant_id"]["nullable"] is False
                assert columns["tenant_id"]["default"] is None
                assert any(
                    tuple(index["column_names"]) == ("tenant_id",)
                    for index in inspector.get_indexes(table)
                )
            execution_uniques = {
                tuple(constraint["column_names"])
                for constraint in inspector.get_unique_constraints("execution_events")
            }
            assert execution_uniques == {("tenant_id", "execution_id")}

        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO execution_events "
                    "(id, tenant_id, execution_id, payload) "
                    "VALUES (2, 'tenant-b', 'shared', 'other')"
                )
            )
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO execution_events "
                    "(id, tenant_id, execution_id, payload) "
                    "VALUES (3, 'tenant-b', 'shared', 'duplicate')"
                )
            )

        engine.dispose()
        engine = None
        with pytest.raises(
            RuntimeError,
            match=r"1 cross-tenant execution_id collision.*shared.*2 tenants.*2 rows",
        ):
            command.downgrade(config, "20260811_04")

        engine = create_engine(database_url)
        with engine.connect() as connection:
            assert {
                tuple(constraint["column_names"])
                for constraint in inspect(connection).get_unique_constraints("execution_events")
            } == {("tenant_id", "execution_id")}
            assert "tenant_id" in {
                column["name"]
                for column in inspect(connection).get_columns("connector_delivery_log")
            }
        with engine.begin() as connection:
            connection.execute(
                text(
                    "DELETE FROM execution_events "
                    "WHERE tenant_id = 'tenant-b' AND execution_id = 'shared'"
                )
            )
        engine.dispose()
        engine = None

        command.downgrade(config, "20260811_04")

        engine = create_engine(database_url)
        with engine.connect() as connection:
            assert {
                tuple(constraint["column_names"])
                for constraint in inspect(connection).get_unique_constraints("execution_events")
            } == {("execution_id",)}
            assert "tenant_id" not in {
                column["name"]
                for column in inspect(connection).get_columns("connector_delivery_log")
            }
            tenant_column = {
                column["name"]: column
                for column in inspect(connection).get_columns("execution_events")
            }["tenant_id"]
            assert tenant_column["default"] is not None
            assert connection.execute(
                text("SELECT tenant_id, execution_id, payload FROM execution_events")
            ).one() == ("default", "shared", "event")
    finally:
        if engine is not None:
            engine.dispose()
        admin_engine.dispose()
        cleanup_engine = create_engine(
            root_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
        )
        with cleanup_engine.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :database_name"
                ),
                {"database_name": database_name},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        cleanup_engine.dispose()


def test_tenant_scope_migration_has_no_permissive_defaults_and_matches_fresh_scope() -> None:
    migrated = create_engine("sqlite+pysqlite:///:memory:")
    with migrated.begin() as connection:
        _legacy_schema(connection)
        migration = _load_migration()
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migrated_shapes = {table: _scope_shape(connection, table) for table in _TABLES}

    fresh = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(fresh)
    fresh_shapes = {table: _scope_shape(fresh, table) for table in _TABLES}

    for table in _TABLES:
        assert migrated_shapes[table]["tenant_nullable"] is False
        assert migrated_shapes[table]["tenant_default"] is None
        assert fresh_shapes[table]["tenant_nullable"] is False
        assert fresh_shapes[table]["tenant_default"] is None
    assert migrated_shapes["execution_events"]["tenant_uniques"] == [("tenant_id", "execution_id")]
    assert fresh_shapes["execution_events"]["tenant_uniques"] == [("tenant_id", "execution_id")]


def test_tenant_scope_migration_declares_postgres_equivalent_plan() -> None:
    migration = _load_migration()

    plan = migration.migration_plan("postgresql")

    assert plan == migration.migration_plan("sqlite")
    assert plan["scope_tables"] == _TABLES
    assert plan["execution_unique"] == ("tenant_id", "execution_id")
    assert plan["normalizes_reserved_default"] is True
    assert plan["removes_server_defaults"] is True


def _recorded_migration_operations(dialect_name: str, monkeypatch) -> list[tuple]:
    migration = _load_migration()
    records: list[tuple] = []

    class Inspector:
        def get_table_names(self):
            return ["execution_events"]

        def get_columns(self, table_name):
            assert table_name == "execution_events"
            return [
                {
                    "name": "tenant_id",
                    "type": migration.sa.String(length=128),
                    "nullable": True,
                    "default": "'tenant_default'",
                },
                {
                    "name": "execution_id",
                    "type": migration.sa.String(length=128),
                    "nullable": False,
                    "default": None,
                },
            ]

        def get_indexes(self, table_name):
            assert table_name == "execution_events"
            return []

        def get_unique_constraints(self, table_name):
            assert table_name == "execution_events"
            return [
                {
                    "name": "uq_execution_events_execution_id",
                    "column_names": ["execution_id"],
                }
            ]

    class Batch:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def alter_column(self, name, **kwargs):
            records.append(("alter_column", name, kwargs["nullable"], kwargs["server_default"]))

        def drop_constraint(self, name, **kwargs):
            records.append(("drop_constraint", name, kwargs["type_"]))

        def create_unique_constraint(self, name, columns):
            records.append(("create_unique", name, tuple(columns)))

    class OperationsHarness:
        class Bind:
            class Dialect:
                name = dialect_name

            dialect = Dialect()

            class Result:
                @staticmethod
                def first():
                    return (1,)

            def execute(self, statement):
                records.append(("probe", str(statement)))
                return self.Result()

        def get_bind(self):
            return self.Bind()

        def execute(self, statement):
            records.append(("execute", str(statement)))

        def batch_alter_table(self, table_name):
            records.append(("batch", table_name))
            return Batch()

        def create_index(self, name, table_name, columns, unique=False):
            records.append(("create_index", name, table_name, tuple(columns), unique))

    monkeypatch.setattr(migration.sa, "inspect", lambda _bind: Inspector())
    migration.op = OperationsHarness()
    migration.upgrade()
    return records


def test_postgres_harness_executes_same_portable_scope_plan_as_sqlite(monkeypatch) -> None:
    postgres_records = _recorded_migration_operations("postgresql", monkeypatch)
    sqlite_records = _recorded_migration_operations("sqlite", monkeypatch)

    assert postgres_records == sqlite_records
    assert any(
        record[0] == "execute" and "tenant_default" in record[1] for record in postgres_records
    )
    assert (
        "create_unique",
        "uq_execution_events_tenant_execution_id",
        ("tenant_id", "execution_id"),
    ) in postgres_records
    assert (
        "alter_column",
        "tenant_id",
        False,
        None,
    ) in postgres_records


def test_sqlite_compatibility_startup_converges_legacy_scope_schema(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        _legacy_schema(connection)
        connection.execute(
            text(
                "INSERT INTO execution_events "
                "(id, tenant_id, execution_id, payload) "
                "VALUES (1, 'tenant_default', 'legacy', 'preserved')"
            )
        )
    monkeypatch.setattr(database_module, "engine", engine)

    database_module._ensure_sqlite_compat()

    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT tenant_id, execution_id, payload FROM execution_events")
        ).one() == ("default", "legacy", "preserved")
        for table in _TABLES:
            shape = _scope_shape(connection, table)
            assert shape["tenant_nullable"] is False
            assert shape["tenant_default"] is None
