from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from zeroth.econ.plane.instrumentation.models import ExecutionEvent
from zeroth.econ.plane import database as database_module


_MICRO_COST = Decimal("0.00000128")


def _migration_config(database_url: str) -> Config:
    root = Path(__file__).parents[2]
    config = Config()
    config.set_main_option("script_location", str(root / "src/zeroth/econ/plane/_migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_execution_event_cost_columns_retain_eight_decimal_places() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    ExecutionEvent.__table__.create(engine)
    try:
        with Session(engine) as session:
            session.add(
                ExecutionEvent(
                    tenant_id="tenant-a",
                    execution_id="micro-cost",
                    join_key="micro-cost",
                    timestamp=datetime(2026, 8, 24, 12, tzinfo=UTC),
                    capability_id="capability-a",
                    implementation_id="implementation-a",
                    model_version="model-a",
                    token_cost_usd=_MICRO_COST,
                    tool_cost_usd=_MICRO_COST,
                    compute_cost_usd=_MICRO_COST,
                    cost_measurement="measured",
                    usage_measurement="measured",
                    event_metadata={},
                )
            )
            session.commit()
            session.expire_all()

            stored = session.execute(select(ExecutionEvent)).scalar_one()

        assert stored.token_cost_usd == _MICRO_COST
        assert stored.tool_cost_usd == _MICRO_COST
        assert stored.compute_cost_usd == _MICRO_COST
        for column_name in (
            "token_cost_usd",
            "tool_cost_usd",
            "compute_cost_usd",
        ):
            column_type = ExecutionEvent.__table__.c[column_name].type
            assert (column_type.precision, column_type.scale) == (18, 8)
    finally:
        engine.dispose()


def test_cost_precision_migration_upgrades_sqlite_and_preserves_micro_cost(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "econ.db"
    url = f"sqlite+pysqlite:///{database}"
    monkeypatch.setenv("ECP_DATABASE_URL", url)
    config = _migration_config(url)
    engine = create_engine(url, future=True)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20260823_09')"))
        connection.execute(
            text(
                "CREATE TABLE execution_events ("
                "id INTEGER PRIMARY KEY, "
                "token_cost_usd NUMERIC(12, 4), "
                "tool_cost_usd NUMERIC(12, 4), "
                "compute_cost_usd NUMERIC(12, 4))"
            )
        )
        connection.execute(
            text(
                "INSERT INTO execution_events "
                "(id, token_cost_usd, tool_cost_usd, compute_cost_usd) "
                "VALUES (1, :cost, :cost, :cost)"
            ),
            {"cost": str(_MICRO_COST)},
        )
    engine.dispose()

    command.upgrade(config, "head")

    engine = create_engine(url, future=True)
    try:
        columns = {
            column["name"]: column for column in inspect(engine).get_columns("execution_events")
        }
        for column_name in (
            "token_cost_usd",
            "tool_cost_usd",
            "compute_cost_usd",
        ):
            column_type = columns[column_name]["type"]
            assert (column_type.precision, column_type.scale) == (18, 8)

        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == "20260901_17"
            )
            costs = connection.execute(
                text(
                    "SELECT token_cost_usd, tool_cost_usd, compute_cost_usd "
                    "FROM execution_events WHERE id = 1"
                )
            ).one()
        assert tuple(Decimal(str(value)) for value in costs) == (_MICRO_COST,) * 3
    finally:
        engine.dispose()


def test_sqlite_runtime_convergence_upgrades_pre_alembic_cost_precision(
    tmp_path: Path, monkeypatch
) -> None:
    """The persistent dev database has no Alembic stamp, so startup owns this path."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'compat.db'}", future=True)
    ExecutionEvent.__table__.create(engine)
    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        with operations.batch_alter_table("execution_events", recreate="always") as batch:
            for column in (
                "token_cost_usd",
                "tool_cost_usd",
                "compute_cost_usd",
            ):
                batch.alter_column(
                    column,
                    existing_type=ExecutionEvent.__table__.c[column].type,
                    type_=database_module.Numeric(12, 4),
                    existing_nullable=True,
                )
    monkeypatch.setattr(database_module, "engine", engine)
    try:
        before = {
            column["name"]: column["type"]
            for column in inspect(engine).get_columns("execution_events")
        }
        assert (before["token_cost_usd"].precision, before["token_cost_usd"].scale) == (12, 4)

        database_module._ensure_sqlite_compat()

        after = {
            column["name"]: column["type"]
            for column in inspect(engine).get_columns("execution_events")
        }
        for column in (
            "token_cost_usd",
            "tool_cost_usd",
            "compute_cost_usd",
        ):
            assert (after[column].precision, after[column].scale) == (18, 8)
    finally:
        engine.dispose()
