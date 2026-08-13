"""A01-11: an enforcement decision transitions the policy action it created.

The two tables used to share no linking column, so ``decide_action`` resolved
the policy row by recency -- approving enforcement action 1 flipped the policy
action proposed for enforcement action 2.  These tests pin the structural link
that replaced the heuristic.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from tests.conftest import requires_docker
from zeroth.econ.plane import database as database_module
from zeroth.econ.plane.capabilities import models as capability_models  # noqa: F401
from zeroth.econ.plane.connectors import models as connector_models  # noqa: F401
from zeroth.econ.plane.counterfactual import (
    models as counterfactual_models,  # noqa: F401
)
from zeroth.econ.plane.database import Base
from zeroth.econ.plane.enforcement.models import (
    AuditLog,
    EnforcementAction,
    PolicyAction,
    TrafficPolicy,
)
from zeroth.econ.plane.enforcement.schemas import EnforcementActionCreate
from zeroth.econ.plane.enforcement.service import create_action, decide_action
from zeroth.econ.plane.instrumentation import (
    models as instrumentation_models,  # noqa: F401
)
from zeroth.econ.plane.performance import models as performance_models  # noqa: F401
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext


@pytest.fixture(autouse=True)
def _connectors_disabled(monkeypatch) -> None:
    """Keep the outbox out of the picture; only the policy transition is under test."""
    monkeypatch.setattr("zeroth.econ.plane.enforcement.service.settings.connectors_enabled", False)


@pytest.fixture
def econ_engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _scope(engine, tenant_id: str) -> tuple[Session, ScopedSession]:
    session = Session(engine)
    return session, ScopedSession(session, TenantWideScopeContext(tenant_id=tenant_id))


def _create(db: ScopedSession, reason: str, capability_id: str = "cap-a") -> EnforcementAction:
    return create_action(
        db,
        EnforcementActionCreate(
            capability_id=capability_id,
            action_type="TriggerInvestigation",
            reason=reason,
        ),
    )


def test_approval_transitions_the_policy_action_created_for_that_enforcement_action(
    econ_engine,
) -> None:
    """AC2: approving enforcement action 1 must not transition policy action 2."""
    raw, tenant_a = _scope(econ_engine, "tenant-a")
    try:
        first = _create(tenant_a, "first")
        _create(tenant_a, "second")
        first_policy, second_policy = tenant_a.scalars(
            select(PolicyAction).order_by(PolicyAction.id)
        ).all()

        decide_action(tenant_a, first.id, "approve", "approver@example.com", "approved")

        raw.expire_all()
        assert (first_policy.status, first_policy.approved_by) == (
            "APPLIED",
            "approver@example.com",
        )
        assert first_policy.applied_at is not None
        assert (second_policy.status, second_policy.approved_by) == ("PROPOSED", None)
        assert (second_policy.approved_at, second_policy.applied_at) == (None, None)
    finally:
        raw.close()


def test_proposed_policy_action_records_its_originating_enforcement_action(econ_engine) -> None:
    raw, tenant_a = _scope(econ_engine, "tenant-a")
    try:
        first = _create(tenant_a, "first")
        second = _create(tenant_a, "second")

        links = [
            (policy.payload_json["reason"], policy.enforcement_action_id)
            for policy in tenant_a.scalars(select(PolicyAction).order_by(PolicyAction.id)).all()
        ]
        assert links == [("first", first.id), ("second", second.id)]
    finally:
        raw.close()


def test_rejection_transitions_only_the_linked_policy_action(econ_engine) -> None:
    raw, tenant_a = _scope(econ_engine, "tenant-a")
    try:
        first = _create(tenant_a, "first")
        _create(tenant_a, "second")
        first_policy, second_policy = tenant_a.scalars(
            select(PolicyAction).order_by(PolicyAction.id)
        ).all()

        decide_action(tenant_a, first.id, "reject", "approver@example.com", "declined")

        raw.expire_all()
        assert (first_policy.status, first_policy.approved_by) == (
            "REJECTED",
            "approver@example.com",
        )
        assert second_policy.status == "PROPOSED"
    finally:
        raw.close()


def test_decision_never_reaches_another_tenants_policy_action(econ_engine) -> None:
    raw_a, tenant_a = _scope(econ_engine, "tenant-a")
    raw_b, tenant_b = _scope(econ_engine, "tenant-b")
    try:
        _create(tenant_b, "foreign")
        foreign_policy = tenant_b.scalars(select(PolicyAction)).one()
        action = _create(tenant_a, "own")

        decide_action(tenant_a, action.id, "approve", "approver@example.com", "approved")

        raw_b.expire_all()
        assert (foreign_policy.status, foreign_policy.approved_by) == ("PROPOSED", None)
    finally:
        raw_a.close()
        raw_b.close()


def _seed_unlinked_legacy_pair(engine, *, action_type: str) -> int:
    """Persist the pre-link shape: an action and a policy row with no link between them."""
    with Session(engine) as seed:
        action = EnforcementAction(
            tenant_id="tenant-a",
            capability_id="cap-a",
            action_type=action_type,
            status="pending",
            reason="legacy",
            before_config={},
            after_config={"impl-a": 100},
            created_at=datetime.now(UTC),
        )
        seed.add(action)
        seed.add(
            PolicyAction(
                tenant_id="tenant-a",
                capability_id="cap-a",
                enforcement_action_id=None,
                proposed_at=datetime.now(UTC),
                proposed_by="system",
                action_type=action_type,
                payload_json={},
                confidence_state_json={},
                status="PROPOSED",
            )
        )
        seed.commit()
        return action.id


def test_unlinked_legacy_policy_action_is_never_resolved_by_recency(econ_engine) -> None:
    """A NULL link means "unlinked", not "the newest row for this capability"."""
    action_id = _seed_unlinked_legacy_pair(econ_engine, action_type="AdjustTrafficWeights")
    raw, tenant_a = _scope(econ_engine, "tenant-a")
    try:
        decided = decide_action(tenant_a, action_id, "approve", "approver@example.com", "approved")

        assert decided is not None
        assert (decided.status, decided.approver_sub) == ("approved", "approver@example.com")
        legacy = tenant_a.scalars(select(PolicyAction)).one()
        assert (legacy.status, legacy.approved_by, legacy.applied_at) == ("PROPOSED", None, None)
        # The applied effect stays coupled to the policy action, so an unlinked
        # decision changes nothing operationally either.
        assert tenant_a.scalars(select(TrafficPolicy)).all() == []
    finally:
        raw.close()


def test_unlinked_decision_is_recorded_as_unlinked_in_the_audit_entry(econ_engine) -> None:
    action_id = _seed_unlinked_legacy_pair(econ_engine, action_type="TriggerInvestigation")
    raw, tenant_a = _scope(econ_engine, "tenant-a")
    try:
        decide_action(tenant_a, action_id, "approve", "approver@example.com", "approved")

        audit = tenant_a.scalars(select(AuditLog)).one()
        assert audit.payload["policy_linkage"] == "unlinked"
        assert audit.payload["policy_action_id"] is None
    finally:
        raw.close()


def test_linked_decision_names_the_policy_action_it_transitioned(econ_engine) -> None:
    raw, tenant_a = _scope(econ_engine, "tenant-a")
    try:
        action = _create(tenant_a, "first")
        policy = tenant_a.scalars(select(PolicyAction)).one()

        decide_action(tenant_a, action.id, "approve", "approver@example.com", "approved")

        audit = tenant_a.scalars(select(AuditLog)).one()
        assert audit.payload["policy_linkage"] == "linked"
        assert audit.payload["policy_action_id"] == policy.id
    finally:
        raw.close()


def test_unlinked_legacy_action_can_still_be_rejected(econ_engine) -> None:
    """Refusing an unlinked decision would make legacy actions impossible to reject."""
    action_id = _seed_unlinked_legacy_pair(econ_engine, action_type="AdjustTrafficWeights")
    raw, tenant_a = _scope(econ_engine, "tenant-a")
    try:
        decided = decide_action(tenant_a, action_id, "reject", "approver@example.com", "declined")

        assert decided is not None
        assert decided.status == "rejected"
    finally:
        raw.close()


def test_link_column_is_nullable_and_carries_no_default() -> None:
    column = PolicyAction.__table__.c.enforcement_action_id
    assert column.nullable is True
    assert column.default is None
    assert column.server_default is None
    assert {foreign_key.target_fullname for foreign_key in column.foreign_keys} == {
        "enforcement_actions.id"
    }


# --------------------------------------------------------------------------
# Schema surfaces: the Alembic revision and the runtime SQLite compat path
# --------------------------------------------------------------------------

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

_LEGACY_ROW = (
    "INSERT INTO policy_actions "
    "(id, tenant_id, capability_id, proposed_at, proposed_by, action_type, payload_json, "
    "confidence_state_json, status) VALUES "
    "(1, 'tenant-a', 'cap-a', '2026-08-11 00:00:00', 'system', 'INVESTIGATION_FLAG', "
    "'{}', '{}', 'PROPOSED')"
)


def _load_migration():
    path = (
        Path(__file__).parents[2]
        / "src/zeroth/econ/plane/_migrations/versions/20260812_06_policy_action_link.py"
    )
    spec = importlib.util.spec_from_file_location("policy_action_link_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _link_column(bind) -> dict[str, object] | None:
    columns = {column["name"]: column for column in inspect(bind).get_columns("policy_actions")}
    return columns.get("enforcement_action_id")


def _econ_config(database_url: str) -> Config:
    root = Path(__file__).parents[2]
    config = Config()
    config.set_main_option("script_location", str(root / "src/zeroth/econ/plane/_migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_link_migration_adds_a_nullable_column_without_backfilling_sqlite_rows() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text(_LEGACY_POLICY_ACTIONS))
        connection.execute(text(_LEGACY_ROW))
        migration = _load_migration()
        migration.op = Operations(MigrationContext.configure(connection))

        migration.upgrade()

        column = _link_column(connection)
        assert column is not None
        assert column["nullable"] is True
        assert column["default"] is None
        assert connection.execute(
            text("SELECT tenant_id, enforcement_action_id FROM policy_actions")
        ).one() == ("tenant-a", None)

        migration.upgrade()  # idempotent
        assert _link_column(connection) is not None

        migration.downgrade()
        assert _link_column(connection) is None
        assert connection.execute(text("SELECT tenant_id FROM policy_actions")).one() == (
            "tenant-a",
        )
        migration.downgrade()  # idempotent
        assert _link_column(connection) is None


def test_migrated_sqlite_link_shape_matches_a_fresh_create_all() -> None:
    migrated = create_engine("sqlite+pysqlite:///:memory:")
    with migrated.begin() as connection:
        connection.execute(text(_LEGACY_POLICY_ACTIONS))
        migration = _load_migration()
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migrated_column = _link_column(connection)

    fresh = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(fresh)
    fresh_column = _link_column(fresh)

    assert migrated_column is not None and fresh_column is not None
    assert migrated_column["nullable"] == fresh_column["nullable"] is True
    assert migrated_column["default"] == fresh_column["default"] is None


def test_link_migration_declares_a_dialect_equivalent_plan() -> None:
    migration = _load_migration()

    plan = migration.migration_plan("postgresql")

    assert plan == migration.migration_plan("sqlite")
    assert plan["table"] == "policy_actions"
    assert plan["column"] == "enforcement_action_id"
    assert plan["nullable"] is True
    assert plan["server_default"] is None
    assert plan["backfills_existing_rows"] is False
    with pytest.raises(ValueError, match="unsupported policy-action link migration dialect"):
        migration.migration_plan("mysql")


def test_alembic_head_adds_the_link_column_to_an_existing_sqlite_revision(
    tmp_path, monkeypatch
) -> None:
    """Also proves the revision chain still resolves to a single head."""
    database_path = tmp_path / "previous.db"
    url = f"sqlite+pysqlite:///{database_path}"
    # env.py prefers ECP_DATABASE_URL over the config url whenever it is set.
    monkeypatch.setenv("ECP_DATABASE_URL", url)
    config = _econ_config(url)
    command.upgrade(config, "20260811_05")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text(_LEGACY_ROW))
        assert _link_column(connection) is None
    engine.dispose()

    command.upgrade(config, "head")

    engine = create_engine(url)
    with engine.connect() as connection:
        assert _link_column(connection) is not None
        assert connection.execute(
            text("SELECT tenant_id, enforcement_action_id FROM policy_actions WHERE id = 1")
        ).one() == ("tenant-a", None)
    engine.dispose()


def test_create_all_alone_cannot_reach_an_existing_sqlite_database(tmp_path, monkeypatch) -> None:
    """The compat ALTER is load-bearing: create_all is create-if-absent, never alter."""
    database_path = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    with engine.begin() as connection:
        connection.execute(text(_LEGACY_POLICY_ACTIONS))
        connection.execute(text(_LEGACY_ROW))

    Base.metadata.create_all(engine)
    with engine.connect() as connection:
        assert _link_column(connection) is None, "create_all must not have altered the table"

    monkeypatch.setattr(database_module, "engine", engine)
    database_module._ensure_sqlite_compat()

    with engine.connect() as connection:
        column = _link_column(connection)
        assert column is not None
        assert column["nullable"] is True
        assert connection.execute(
            text("SELECT tenant_id, enforcement_action_id FROM policy_actions WHERE id = 1")
        ).one() == ("tenant-a", None)
    database_module._ensure_sqlite_compat()  # idempotent
    engine.dispose()


@pytest.mark.postgres
@requires_docker
def test_link_migration_executes_on_live_postgres(postgres_container, monkeypatch) -> None:
    root_url = make_url(postgres_container.get_connection_url().replace("psycopg2", "psycopg"))
    database_name = f"econ_policy_link_{uuid4().hex[:10]}"
    admin_engine = create_engine(root_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    database_url = root_url.set(database=database_name).render_as_string(hide_password=False)
    monkeypatch.setenv("ECP_DATABASE_URL", database_url)
    engine = None
    try:
        config = _econ_config(database_url)
        command.upgrade(config, "20260811_05")
        engine = create_engine(database_url)
        with engine.begin() as connection:
            connection.execute(text(_LEGACY_ROW))
            assert _link_column(connection) is None
        engine.dispose()
        engine = None

        command.upgrade(config, "head")

        engine = create_engine(database_url)
        with engine.connect() as connection:
            column = _link_column(connection)
            assert column is not None
            assert column["nullable"] is True
            assert column["default"] is None
            assert connection.execute(
                text("SELECT tenant_id, enforcement_action_id FROM policy_actions WHERE id = 1")
            ).one() == ("tenant-a", None)
        engine.dispose()
        engine = None

        command.downgrade(config, "20260811_05")

        engine = create_engine(database_url)
        with engine.connect() as connection:
            assert _link_column(connection) is None
            assert connection.execute(
                text("SELECT tenant_id FROM policy_actions WHERE id = 1")
            ).one() == ("tenant-a",)
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
