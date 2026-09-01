"""ZER-49 F-06: the runtime convergence door existed only for SQLite.

``_ensure_sqlite_compat`` returned immediately on any other dialect,
``create_all`` is create-if-absent and never alters an existing table, and
``_migrations/`` is offline tooling runtime never invokes (``PROVENANCE.md``).
So a PostgreSQL econ database that predates a revision had no door at all: the
mapper declared a column the table did not have, and every read through it
failed with ``UndefinedColumn`` -- ``list_policy_actions``, ``_propose_policy_action``,
``_linked_policy_action`` and the dashboard timeline alike.

The remedy is to refuse rather than to converge.  On PostgreSQL the Alembic
chain owns the schema; issuing the ALTER from application code instead would
take DDL locks from every replica that starts, need a grant the application role
often lacks, and -- decisively -- leave ``alembic_version`` untouched, so the
schema-revision evidence on the health path would report "behind" for a schema
that had been silently patched.  These tests pin the refusal, and pin that the
remedy it names is the remedy that actually works.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from tests.conftest import requires_docker
from zeroth.econ.plane import database as database_module
from zeroth.econ.plane.auth import models as auth_models  # noqa: F401
from zeroth.econ.plane.capabilities import models as capability_models  # noqa: F401
from zeroth.econ.plane.cloud import models as cloud_models  # noqa: F401
from zeroth.econ.plane.connectors import models as connector_models  # noqa: F401
from zeroth.econ.plane.costing import models as costing_models  # noqa: F401
from zeroth.econ.plane.counterfactual import (
    models as counterfactual_models,  # noqa: F401
)
from zeroth.econ.plane.dashboard import models as dashboard_models  # noqa: F401
from zeroth.econ.plane.database import Base, EconSchemaNotConverged
from zeroth.econ.plane.enforcement.models import PolicyAction
from zeroth.econ.plane.instrumentation import (
    models as instrumentation_models,  # noqa: F401
)
from zeroth.econ.plane.performance import models as performance_models  # noqa: F401
from zeroth.econ.plane.reconciliation import (
    models as reconciliation_models,  # noqa: F401
)
from zeroth.econ.plane.statistics import models as statistics_models  # noqa: F401

_LEGACY_POLICY_ACTIONS = """
CREATE TABLE policy_actions (
    id INTEGER PRIMARY KEY,
    tenant_id VARCHAR(128) NOT NULL DEFAULT 'tenant_default',
    capability_id VARCHAR(128) NOT NULL,
    proposed_at DATETIME NOT NULL,
    proposed_by VARCHAR(128) NOT NULL,
    action_type VARCHAR(64) NOT NULL,
    payload_json JSON NOT NULL,
    metrics_snapshot_id INTEGER,
    confidence_state_json JSON NOT NULL,
    status VARCHAR(32) NOT NULL,
    approved_by VARCHAR(128),
    approved_at DATETIME,
    applied_at DATETIME,
    failure_reason VARCHAR(512)
)
"""

# The shape execution_events has in every real database at revision
# 20260811_04: nothing in the chain creates it -- create_all does, from a model
# that has carried the three cost columns since the table was introduced -- and
# 20260812_04 is the revision that relaxes them and adds the measurement
# provenance this test expects to be reported as missing.
_PRE_MEASUREMENT_EXECUTION_EVENTS = (
    "CREATE TABLE execution_events ("
    "id INTEGER PRIMARY KEY, "
    "tenant_id VARCHAR(128) DEFAULT 'tenant_default', "
    "execution_id VARCHAR(128) NOT NULL, "
    "token_cost_usd NUMERIC(12, 4) NOT NULL, "
    "tool_cost_usd NUMERIC(12, 4) NOT NULL, "
    "compute_cost_usd NUMERIC(12, 4) NOT NULL, "
    "CONSTRAINT uq_execution_events_execution_id UNIQUE (execution_id))"
)

_PRE_BILLING_SYNC_SUBSCRIPTIONS = (
    "CREATE TABLE cloud_subscriptions ("
    "tenant_id VARCHAR(128) PRIMARY KEY, "
    "plan VARCHAR(32) NOT NULL, "
    "status VARCHAR(32) NOT NULL, "
    "period_start DATETIME NOT NULL, "
    "period_end DATETIME NOT NULL, "
    "external_customer_id VARCHAR(128), "
    "external_subscription_id VARCHAR(128), "
    "updated_at DATETIME NOT NULL)"
)


def _econ_config(database_url: str) -> Config:
    root = Path(__file__).parents[2]
    config = Config()
    config.set_main_option("script_location", str(root / "src/zeroth/econ/plane/_migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_every_declared_chain_owned_column_is_one_the_runtime_maps() -> None:
    """The declaration cannot name a column no mapper reads: that is not drift.

    The refusal exists because a mapper emits SQL naming a column the table does
    not have.  A declared entry that no model declares would refuse startup over
    a column nothing would ever have selected.
    """
    for table, column, revision in database_module._CHAIN_OWNED_COLUMNS:
        assert table in Base.metadata.tables, table
        assert column in Base.metadata.tables[table].columns, f"{table}.{column}"
        assert revision.startswith("2026"), revision


def test_an_absent_table_is_not_drift_but_an_unaltered_one_is() -> None:
    """Detection is dialect-agnostic; only the refusal is gated on the dialect.

    A table that is absent entirely has nothing to converge -- ``create_all``
    runs first and builds it from the model, columns and all.  Drift is the
    table that already existed and that ``create_all``, being create-if-absent,
    skipped wholesale.
    """
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    try:
        with engine.begin() as connection:
            assert database_module._missing_chain_owned_columns(connection) == ()

            connection.execute(text(_LEGACY_POLICY_ACTIONS))
            assert database_module._missing_chain_owned_columns(connection) == (
                ("policy_actions", "enforcement_action_id", "20260812_06"),
            )

            connection.execute(
                text("ALTER TABLE policy_actions ADD COLUMN enforcement_action_id INTEGER")
            )
            assert database_module._missing_chain_owned_columns(connection) == ()
    finally:
        engine.dispose()


def test_sqlite_converges_instead_of_refusing(tmp_path: Path, monkeypatch) -> None:
    """The refusal must not reach the dialect that ships no migration step.

    SQLite is the default econ database, has no ``alembic upgrade`` in its
    documented lifecycle, and is owned by one process -- so it keeps the
    application-issued ALTERs, and the same un-converged shape that refuses on
    PostgreSQL has to converge here.
    """
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'econ.db'}", future=True)
    monkeypatch.setattr(database_module, "engine", engine)
    try:
        with engine.begin() as connection:
            connection.execute(text(_LEGACY_POLICY_ACTIONS))
            connection.execute(text(_PRE_BILLING_SYNC_SUBSCRIPTIONS))
            assert database_module._missing_chain_owned_columns(connection) != ()
        Base.metadata.create_all(bind=engine)

        database_module._ensure_sqlite_compat()

        with engine.connect() as connection:
            assert database_module._missing_chain_owned_columns(connection) == ()
            subscription_columns = {
                column["name"]
                for column in inspect(connection).get_columns("cloud_subscriptions")
            }
            assert "billing_provider" in subscription_columns
            assert "last_billing_event_at" in subscription_columns
    finally:
        engine.dispose()


@pytest.mark.postgres
@requires_docker
def test_non_sqlite_startup_refuses_a_schema_the_chain_has_not_reached(
    postgres_container, monkeypatch
) -> None:
    """The measured F-06 break, and the refusal that replaces it.

    Also the oracle for the remedy line: every column the refusal names must be
    one ``alembic upgrade head`` supplies.  Most of what the SQLite branch
    patches is added by no revision at all, so a general ORM-versus-database
    diff would name a remedy that does not work.
    """
    root_url = make_url(postgres_container.get_connection_url().replace("psycopg2", "psycopg"))
    database_name = f"econ_convergence_{uuid4().hex[:10]}"
    admin_engine = create_engine(root_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    database_url = root_url.set(database=database_name).render_as_string(hide_password=False)
    monkeypatch.setenv("ECP_DATABASE_URL", database_url)
    config = _econ_config(database_url)
    engine = None
    try:
        command.upgrade(config, "20260811_04")
        engine = create_engine(database_url, future=True)
        with engine.begin() as connection:
            connection.execute(text(_PRE_MEASUREMENT_EXECUTION_EVENTS))
        monkeypatch.setattr(database_module, "engine", engine)

        # bootstrap()'s schema prefix, in order.
        Base.metadata.create_all(bind=engine)

        # Negative control: create_all left the pre-existing tables alone, so the
        # mapper names a column that is not there and the read is a hard break --
        # not a degradation.
        with Session(engine) as session, pytest.raises(ProgrammingError) as undefined:
            session.execute(select(PolicyAction)).scalars().all()
        assert "enforcement_action_id" in str(undefined.value)

        with pytest.raises(EconSchemaNotConverged) as refusal:
            database_module._ensure_sqlite_compat()

        message = str(refusal.value)
        for expected in (
            "connector_delivery_log.tenant_id (revision 20260811_05)",
            "execution_events.cost_measurement (revision 20260812_04)",
            "execution_events.usage_measurement (revision 20260812_04)",
            "policy_actions.enforcement_action_id (revision 20260812_06)",
            "'20260811_04'",  # applied
            "'20260901_16'",  # shipped head
            "behind",
            "alembic upgrade head",
            "postgresql",
        ):
            assert expected in message, message
        engine.dispose()
        engine = None

        # The remedy the refusal names is the remedy that works: every column it
        # reported is supplied by the chain, and startup then goes through.
        command.upgrade(config, "head")

        engine = create_engine(database_url, future=True)
        monkeypatch.setattr(database_module, "engine", engine)
        Base.metadata.create_all(bind=engine)
        with engine.connect() as connection:
            assert database_module._missing_chain_owned_columns(connection) == ()

        database_module._ensure_sqlite_compat()

        with Session(engine) as session:
            assert session.execute(select(PolicyAction)).scalars().all() == []
        # ...and the link column arrived carrying the reference, because
        # create_all had already built its target before the chain ran.
        with engine.connect() as connection:
            links = [
                (key["referred_table"], tuple(key["constrained_columns"]))
                for key in inspect(connection).get_foreign_keys("policy_actions")
            ]
        assert ("enforcement_actions", ("enforcement_action_id",)) in links
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
