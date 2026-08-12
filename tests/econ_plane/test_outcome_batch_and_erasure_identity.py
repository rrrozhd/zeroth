"""ZER-49 P8: transactional identity for outcome ingestion and econ erasure.

A01-38 -- ``POST /v1/outcomes/ingest`` used to commit every event of a batch on
its own, so a rejection on event N left events 1..N-1 durably stored behind a 422
the caller could not attribute.  Making the batch one transaction is half the
fix: ``OutcomeEvent`` also carried no uniqueness key at all (``ExecutionEvent``
has ``uq_execution_events_tenant_execution_id``), so a client retrying a batch
whose response it never saw simply duplicated the outcomes.

A01-48 -- the erasure adapter's deletes and its receipt insert already shared one
commit, so it was never a partial-write defect.  The real defect is a race on the
idempotency key: two callers both read no receipt, both delete, and only one can
own the primary key.
"""

from __future__ import annotations

import importlib.util
import threading
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from tests.conftest import requires_docker
from zeroth.econ.plane import database as econ_database
from zeroth.econ.plane.auth.deps import get_current_scoped_db, get_current_user
from zeroth.econ.plane.auth.scoped import ScopedUserClaims
from zeroth.econ.plane.capabilities.models import Capability, Implementation
from zeroth.econ.plane.database import Base
from zeroth.econ.plane.erasure import SqlAlchemyEconEventEraser
from zeroth.econ.plane.instrumentation.api import router as instrumentation_router
from zeroth.econ.plane.instrumentation.models import (
    EconErasureReceipt,
    ExecutionEvent,
    OutcomeEvent,
)
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext

_NOW = datetime(2026, 8, 12, tzinfo=UTC)
_TENANT = "tenant-a"


# --------------------------------------------------------------------------
# A01-38: one transaction per batch, and a durable outcome identity
# --------------------------------------------------------------------------


@pytest.fixture
def econ_engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as seed:
        seed.add_all(
            [
                Capability(id="cap-a", tenant_id=_TENANT, name="A"),
                Implementation(
                    id="impl-a",
                    tenant_id=_TENANT,
                    capability_id="cap-a",
                    name="A implementation",
                ),
            ]
        )
        seed.commit()
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def client(econ_engine) -> TestClient:
    app = FastAPI()
    app.include_router(instrumentation_router, prefix="/v1")

    def scoped_db():
        with Session(econ_engine) as db:
            yield ScopedSession(db, TenantWideScopeContext(tenant_id=_TENANT))

    app.dependency_overrides[get_current_scoped_db] = scoped_db
    app.dependency_overrides[get_current_user] = lambda: ScopedUserClaims(
        sub="analyst",
        email="analyst@example.com",
        roles=["Analyst"],
        tenant_id=_TENANT,
        exp=2_000_000_000,
        iss="test",
    )
    return TestClient(app)


def _event(index: int, *, capability_id: str = "cap-a") -> dict:
    return {
        "join_key": f"case-{index}",
        "capability_id": capability_id,
        "implementation_id": "impl-a",
        "outcome_type": "conversion",
        "outcome_value": True,
        "occurred_at": _NOW.isoformat(),
    }


def _stored_outcomes(engine) -> list[str]:
    with Session(engine) as read:
        return sorted(read.scalars(select(OutcomeEvent.join_key)).all())


def test_rejected_batch_event_leaves_no_earlier_event_committed(econ_engine, client) -> None:
    """A01-38: event 7 of 10 raising must not leave events 1-6 in the database."""
    events = [_event(index) for index in range(1, 11)]
    events[6] = _event(7, capability_id="capability-that-does-not-exist")

    response = client.post("/v1/outcomes/ingest", json={"events": events})

    assert response.status_code == 422
    assert response.json() == {"detail": "capability does not exist in the bound tenant"}
    # The whole point: a 422 means "none of these landed", not "six of them did".
    assert _stored_outcomes(econ_engine) == []


def test_retrying_a_batch_does_not_duplicate_already_stored_outcomes(
    econ_engine, client
) -> None:
    """A01-38: without an outcome identity a retried batch doubled every row."""
    events = [_event(index) for index in range(1, 4)]

    first = client.post("/v1/outcomes/ingest", json={"events": events})
    retry = client.post("/v1/outcomes/ingest", json={"events": events})

    assert first.status_code == 200
    assert [item["status"] for item in first.json()] == ["inserted"] * 3
    assert retry.status_code == 200
    assert [item["status"] for item in retry.json()] == ["duplicate"] * 3
    assert _stored_outcomes(econ_engine) == ["case-1", "case-2", "case-3"]


def test_batch_retry_after_a_partial_failure_stores_each_outcome_once(
    econ_engine, client
) -> None:
    """The two halves together: fail, fix the bad event, retry, no duplicates."""
    events = [_event(index) for index in range(1, 6)]
    broken = [*events]
    broken[3] = _event(4, capability_id="capability-that-does-not-exist")

    failed = client.post("/v1/outcomes/ingest", json={"events": broken})
    repaired = client.post("/v1/outcomes/ingest", json={"events": events})

    assert failed.status_code == 422
    assert repaired.status_code == 200
    assert _stored_outcomes(econ_engine) == [f"case-{index}" for index in range(1, 6)]


def test_a_repeat_inside_one_batch_is_reported_rather_than_stored_twice(
    econ_engine, client
) -> None:
    """The batch is one transaction, so a repeat must resolve without aborting it."""
    events = [_event(1), _event(2), _event(1)]

    response = client.post("/v1/outcomes/ingest", json={"events": events})

    assert response.status_code == 200
    assert [item["status"] for item in response.json()] == [
        "inserted",
        "inserted",
        "duplicate",
    ]
    assert _stored_outcomes(econ_engine) == ["case-1", "case-2"]


def test_outcome_identity_is_unique_at_the_database(econ_engine) -> None:
    """The service pre-check is not the guard; the index is."""

    def row(implementation_id: str | None) -> OutcomeEvent:
        return OutcomeEvent(
            tenant_id=_TENANT,
            join_key="case-1",
            execution_id="case-1",
            capability_id="cap-a",
            implementation_id=implementation_id,
            outcome_type="conversion",
            outcome_payload_json={},
            occurred_at=_NOW,
            ingested_at=_NOW,
            outcome_timestamp=_NOW,
        )

    with Session(econ_engine) as first:
        first.add(row(None))
        first.commit()
    with pytest.raises(IntegrityError):
        with Session(econ_engine) as second:
            # NULL implementation_id must NOT exempt a row from the identity --
            # an unlinked outcome is the most common batch shape.
            second.add(row(None))
            second.commit()


# --------------------------------------------------------------------------
# A01-48: the erasure idempotency race
# --------------------------------------------------------------------------


def _seed_erasable_events(engine, *, join_key: str, count: int) -> None:
    with Session(engine) as seed:
        for index in range(count):
            seed.add(
                ExecutionEvent(
                    tenant_id=_TENANT,
                    execution_id=f"exec-{index}",
                    join_key=join_key,
                    timestamp=_NOW,
                    capability_id="cap-a",
                    implementation_id="impl-a",
                    model_version="v1",
                )
            )
            seed.add(
                OutcomeEvent(
                    tenant_id=_TENANT,
                    join_key=join_key,
                    execution_id=f"exec-{index}",
                    capability_id="cap-a",
                    implementation_id="impl-a",
                    outcome_type="conversion",
                    outcome_payload_json={},
                    occurred_at=datetime(2026, 8, 12, 0, index, tzinfo=UTC),
                    ingested_at=_NOW,
                    outcome_timestamp=_NOW,
                )
            )
        seed.commit()


def test_concurrent_erasure_with_one_idempotency_key_erases_once(
    tmp_path: Path, monkeypatch
) -> None:
    """A01-48: two callers holding the same key must not race on the receipt.

    Both callers are held at the receipt read by a barrier, so both genuinely
    observe "no receipt yet" -- the exact interleaving the defect needs.  A
    file-backed database with a busy timeout is what makes the two connections
    independent; the shared-connection in-memory fixtures above cannot express
    concurrency at all.
    """
    database = tmp_path / "econ-erasure-race.db"
    engine = create_engine(
        f"sqlite+pysqlite:///{database}",
        future=True,
        connect_args={"timeout": 30},
    )
    Base.metadata.create_all(engine)
    _seed_erasable_events(engine, join_key="case-42", count=3)

    barrier = threading.Barrier(2)
    armed_on: list[str] = []
    lost_claim: list[str] = []

    class _BarrierSession(Session):
        """Release both callers only once both have read an empty receipt."""

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self._armed = True

        def get(self, *args, **kwargs):
            result = super().get(*args, **kwargs)
            if self._armed:
                self._armed = False
                armed_on.append(args[0].__name__)
                barrier.wait(timeout=30)
            return result

        def flush(self, *args, **kwargs):
            try:
                return super().flush(*args, **kwargs)
            except IntegrityError:
                lost_claim.append(threading.current_thread().name)
                raise

    monkeypatch.setattr(
        econ_database,
        "SessionLocal",
        sessionmaker(bind=engine, autocommit=False, autoflush=False, class_=_BarrierSession),
    )

    eraser = SqlAlchemyEconEventEraser()
    returned: dict[int, int] = {}
    errors: list[BaseException] = []

    def erase(slot: int) -> None:
        try:
            returned[slot] = eraser._delete_sync(_TENANT, ["case-42"], "operation-1")
        except BaseException as exc:  # noqa: BLE001 - the assertion is on the list
            errors.append(exc)
            barrier.abort()  # never strand the peer waiting on a caller that died

    threads = [threading.Thread(target=erase, args=(slot,)) for slot in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    try:
        # The barrier has to be the receipt read specifically, and both callers
        # have to reach it -- otherwise the test proves nothing about the race.
        assert armed_on == ["EconErasureReceipt", "EconErasureReceipt"]
        # Exactly one caller lost the claim. Without this the test would still
        # pass if the two callers merely serialized and never raced at all.
        assert len(lost_claim) == 1, f"expected one lost claim, got {lost_claim!r}"
        assert errors == [], f"a concurrent caller failed: {errors!r}"
        assert set(returned) == {0, 1}
        # Both callers report the same erasure: one performed it, one replayed it.
        assert returned[0] == returned[1] == 6
        with Session(engine) as read:
            assert read.scalar(select(func.count()).select_from(EconErasureReceipt)) == 1
            receipt = read.scalars(select(EconErasureReceipt)).one()
            assert receipt.deleted_count == 6
            assert receipt.tenant_id == _TENANT
            # Deleted exactly once -- the loser must not have re-counted anything.
            assert read.scalar(select(func.count()).select_from(ExecutionEvent)) == 0
            assert read.scalar(select(func.count()).select_from(OutcomeEvent)) == 0
    finally:
        engine.dispose()


def test_sequential_erasure_replay_is_unchanged(tmp_path: Path, monkeypatch) -> None:
    """Claiming the key first must not break the ordinary replay path."""
    database = tmp_path / "econ-erasure-replay.db"
    engine = create_engine(f"sqlite+pysqlite:///{database}", future=True)
    Base.metadata.create_all(engine)
    _seed_erasable_events(engine, join_key="case-7", count=2)
    monkeypatch.setattr(
        econ_database,
        "SessionLocal",
        sessionmaker(bind=engine, autocommit=False, autoflush=False, class_=Session),
    )

    eraser = SqlAlchemyEconEventEraser()
    first = eraser._delete_sync(_TENANT, ["case-7"], "operation-2")
    second = eraser._delete_sync(_TENANT, ["case-7"], "operation-2")

    assert first == second == 4
    with Session(engine) as read:
        assert read.scalar(select(func.count()).select_from(EconErasureReceipt)) == 1
    with pytest.raises(ValueError, match="tenant mismatch"):
        eraser._delete_sync("tenant-b", ["case-7"], "operation-2")
    engine.dispose()


def test_failed_erasure_leaves_the_idempotency_key_free(tmp_path: Path, monkeypatch) -> None:
    """The claim is not durable until the deletes commit with it."""
    database = tmp_path / "econ-erasure-failure.db"
    engine = create_engine(f"sqlite+pysqlite:///{database}", future=True)
    Base.metadata.create_all(engine)
    _seed_erasable_events(engine, join_key="case-9", count=1)

    exploded = {"done": False}

    class _FailOnceSession(Session):
        def execute(self, *args, **kwargs):
            if not exploded["done"]:
                exploded["done"] = True
                raise RuntimeError("delete failed")
            return super().execute(*args, **kwargs)

    monkeypatch.setattr(
        econ_database,
        "SessionLocal",
        sessionmaker(bind=engine, autocommit=False, autoflush=False, class_=_FailOnceSession),
    )
    eraser = SqlAlchemyEconEventEraser()

    with pytest.raises(RuntimeError, match="delete failed"):
        eraser._delete_sync(_TENANT, ["case-9"], "operation-3")

    # No half-claimed receipt survived, so the retry does the real work.
    with Session(engine) as read:
        assert read.scalar(select(func.count()).select_from(EconErasureReceipt)) == 0
    assert eraser._delete_sync(_TENANT, ["case-9"], "operation-3") == 2
    engine.dispose()


# --------------------------------------------------------------------------
# Revision 20260812_07: the identity on databases create_all will never revisit
# --------------------------------------------------------------------------

_LEGACY_OUTCOME_TABLE = (
    "CREATE TABLE outcome_events ("
    "id INTEGER PRIMARY KEY, "
    "tenant_id VARCHAR(128) NOT NULL, "
    "join_key VARCHAR(128), "
    "execution_id VARCHAR(128), "
    "capability_id VARCHAR(128), "
    "implementation_id VARCHAR(128), "
    "outcome_type VARCHAR(64), "
    "occurred_at TIMESTAMP)"
)
_INSERT_OUTCOME = (
    "INSERT INTO outcome_events "
    "(id, tenant_id, join_key, execution_id, capability_id, implementation_id, "
    "outcome_type, occurred_at) VALUES "
    "(:id, :tenant_id, :join_key, :join_key, 'cap-a', :implementation_id, "
    "'conversion', :occurred_at)"
)
_OCCURRED = "2026-08-12 00:00:00"


def _load_identity_migration():
    path = (
        Path(__file__).parents[2]
        / "src/zeroth/econ/plane/_migrations/versions/20260812_07_outcome_event_identity.py"
    )
    spec = importlib.util.spec_from_file_location("outcome_identity_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_identity_migration(connection, *, downgrade: bool = False) -> None:
    migration = _load_identity_migration()
    migration.op = Operations(MigrationContext.configure(connection))
    migration.downgrade() if downgrade else migration.upgrade()


def _outcome(id_: int, join_key: str, implementation_id: str | None) -> dict:
    return {
        "id": id_,
        "tenant_id": _TENANT,
        "join_key": join_key,
        "implementation_id": implementation_id,
        "occurred_at": _OCCURRED,
    }


def test_identity_migration_declares_the_same_plan_for_both_dialects() -> None:
    migration = _load_identity_migration()

    plan = migration.migration_plan("postgresql")

    assert plan == migration.migration_plan("sqlite")
    assert plan["identity_columns"] == ("tenant_id", "join_key", "outcome_type", "occurred_at")
    assert plan["implementation_expression"] == "coalesce(implementation_id, '')"
    assert plan["refuses_existing_duplicates"] is True
    # An econ table under right-to-erasure audit: the migration never resolves a
    # collision by deleting a row.
    assert plan["deletes_rows"] is False
    with pytest.raises(ValueError, match="unsupported outcome-identity migration dialect"):
        migration.migration_plan("mysql")


def test_identity_migration_upgrades_a_legacy_sqlite_table_and_round_trips() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text(_LEGACY_OUTCOME_TABLE))
        connection.execute(text(_INSERT_OUTCOME), _outcome(1, "case-1", None))
        connection.execute(text(_INSERT_OUTCOME), _outcome(2, "case-1", "impl-a"))

        _run_identity_migration(connection)

        assert connection.execute(text("SELECT COUNT(*) FROM outcome_events")).scalar() == 2
        with pytest.raises(IntegrityError):
            connection.execute(text(_INSERT_OUTCOME), _outcome(3, "case-1", None))

    with engine.begin() as connection:
        _run_identity_migration(connection, downgrade=True)
        connection.execute(text(_INSERT_OUTCOME), _outcome(4, "case-1", None))
        assert connection.execute(text("SELECT COUNT(*) FROM outcome_events")).scalar() == 3
    engine.dispose()


def test_identity_migration_refuses_a_table_that_already_holds_duplicates() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text(_LEGACY_OUTCOME_TABLE))
        connection.execute(text(_INSERT_OUTCOME), _outcome(1, "case-1", None))
        connection.execute(text(_INSERT_OUTCOME), _outcome(2, "case-1", None))

        with pytest.raises(RuntimeError, match=r"1 duplicate outcome identit.*case-1.*2 rows"):
            _run_identity_migration(connection)

        # Refused before any DDL, and nothing was deleted to force it through.
        assert connection.execute(text("SELECT COUNT(*) FROM outcome_events")).scalar() == 2
        assert "uq_outcome_events_tenant_identity" not in {
            row[0]
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'index'")
            )
        }
    engine.dispose()


def test_identity_migration_skips_a_schema_without_the_identity_columns() -> None:
    """outcome_events is not built by this chain, so it may be absent or partial."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        _run_identity_migration(connection)  # no table at all
        connection.execute(
            text("CREATE TABLE outcome_events (id INTEGER PRIMARY KEY, tenant_id VARCHAR(128))")
        )
        _run_identity_migration(connection)  # table without the identity columns
        assert "uq_outcome_events_tenant_identity" not in {
            row[0]
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'index'")
            )
        }
    engine.dispose()


@pytest.mark.postgres
@requires_docker
def test_identity_migration_executes_on_live_postgres(postgres_container) -> None:
    root_url = make_url(postgres_container.get_connection_url().replace("psycopg2", "psycopg"))
    database_name = f"econ_outcome_identity_{uuid4().hex[:10]}"
    admin_engine = create_engine(root_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    database_url = root_url.set(database=database_name).render_as_string(hide_password=False)
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(text(_LEGACY_OUTCOME_TABLE))
            connection.execute(text(_INSERT_OUTCOME), _outcome(1, "case-1", None))
            connection.execute(text(_INSERT_OUTCOME), _outcome(2, "case-1", None))
            with pytest.raises(RuntimeError, match=r"1 duplicate outcome identit"):
                _run_identity_migration(connection)

        with engine.begin() as connection:
            connection.execute(text("DELETE FROM outcome_events WHERE id = 2"))
            connection.execute(text(_INSERT_OUTCOME), _outcome(2, "case-1", "impl-a"))
            _run_identity_migration(connection)
            assert "uq_outcome_events_tenant_identity" in {
                index["name"] for index in inspect(connection).get_indexes("outcome_events")
            }

        # NULL implementation_id must collide on PostgreSQL exactly as on SQLite.
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(text(_INSERT_OUTCOME), _outcome(3, "case-1", None))
        with engine.begin() as connection:
            assert connection.execute(text("SELECT COUNT(*) FROM outcome_events")).scalar() == 2
            _run_identity_migration(connection, downgrade=True)
            connection.execute(text(_INSERT_OUTCOME), _outcome(4, "case-1", None))
            assert connection.execute(text("SELECT COUNT(*) FROM outcome_events")).scalar() == 3
    finally:
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
