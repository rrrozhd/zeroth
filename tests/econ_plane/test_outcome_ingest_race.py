"""F-11: the outcome identity pre-check is a pre-check, not the guard.

``_existing_outcome`` turns a *sequential* retry into a reported duplicate.  It
cannot turn a *concurrent* one into anything: between its SELECT and the
``db.flush()`` that follows there is no lock and no ``ON CONFLICT``, so two
identical ingests both miss it and the loser's flush raises ``IntegrityError``.
That is not a ``ValueError``, so neither ``post_outcome`` nor
``ingest_outcome_batch`` caught it -- the loser got an HTTP 500, and on the batch
endpoint the blanket ``except Exception: db.rollback()`` in ``ingest_outcomes``
took every event of the batch down with it.

Every race here is driven at the HTTP boundary, because that is where the defect
surfaces.  The existing ``pytest.raises(IntegrityError)`` tests assert that the
index fires and say nothing at all about what the caller is told.

The mid-batch shape -- a collision on event N with events 1..N-1 already
accepted -- is reachable only on PostgreSQL and lives in the ``postgres`` test at
the bottom: on SQLite the batch holds the single write lock from its first flush
onward, so no rival can commit a colliding row after event 1.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from tests.conftest import requires_docker
from zeroth.econ.plane.auth.deps import get_current_scoped_db, get_current_user
from zeroth.econ.plane.auth.scoped import ScopedUserClaims
from zeroth.econ.plane.capabilities.models import Capability, Implementation
from zeroth.econ.plane.database import Base
from zeroth.econ.plane.instrumentation import service
from zeroth.econ.plane.instrumentation.api import router as instrumentation_router
from zeroth.econ.plane.instrumentation.models import OutcomeEvent
from zeroth.econ.plane.instrumentation.service import _existing_outcome
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext

_NOW = datetime(2026, 8, 12, tzinfo=UTC)
_TENANT = "tenant-a"
_WAIT = 30


def _seed_capability(engine) -> None:
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


def _event(index: int) -> dict:
    return {
        "join_key": f"case-{index}",
        "capability_id": "cap-a",
        "implementation_id": "impl-a",
        "outcome_type": "conversion",
        "outcome_value": True,
        "occurred_at": _NOW.isoformat(),
    }


def _stored(engine) -> list[str]:
    with Session(engine) as read:
        return sorted(read.scalars(select(OutcomeEvent.join_key)).all())


def _plain_sessionmaker(engine):
    return sessionmaker(bind=engine, autocommit=False, autoflush=False, class_=Session)


def _held_sessionmaker(engine, hold, *, after: int = 1, lost: list[str] | None = None):
    """Sessions that run ``hold()`` once, just before their ``after``-th flush.

    The pre-check SELECT for that event has already run by the time ``hold`` is
    called, so a caller parked here has genuinely observed "no such outcome" --
    which is the whole precondition of the race.  On SQLite it also matters that
    pysqlite defers ``BEGIN`` to the first DML: a caller parked before its *first*
    flush has issued nothing but SELECTs and therefore holds no write lock
    against its peers.

    ``lost`` collects the callers whose flush actually hit the constraint.  A test
    that only asserts the reported statuses would pass just as well if the two
    callers had quietly serialized and resolved through the ordinary pre-check
    path, never racing at all -- so the races here measure the collision instead
    of inferring it from the harness.
    """

    class _HeldSession(Session):
        _flushes = 0
        _fired = False

        def flush(self, *args, **kwargs):
            self._flushes = self._flushes + 1
            if self._flushes == after and not self._fired:
                self._fired = True
                hold()
            try:
                return super().flush(*args, **kwargs)
            except IntegrityError:
                if lost is not None:
                    lost.append(threading.current_thread().name)
                raise

    return sessionmaker(bind=engine, autocommit=False, autoflush=False, class_=_HeldSession)


def _app(session_factory) -> FastAPI:
    app = FastAPI()
    app.include_router(instrumentation_router, prefix="/v1")

    def scoped_db():
        with session_factory() as db:
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
    return app


def _run(*targets) -> None:
    threads = [threading.Thread(target=target, name=target.__name__) for target in targets]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=90)
    assert not any(thread.is_alive() for thread in threads), "a concurrent caller never finished"


@pytest.fixture
def race_engine(tmp_path):
    """A file-backed database with a busy timeout: two genuinely separate connections.

    The shared in-memory ``StaticPool`` fixtures elsewhere in this suite hand every
    caller the *same* connection and so cannot express concurrency at all.
    """
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'econ-outcome-race.db'}",
        future=True,
        connect_args={"timeout": _WAIT, "check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    _seed_capability(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def memory_engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    _seed_capability(engine)
    try:
        yield engine
    finally:
        engine.dispose()


# --------------------------------------------------------------------------
# (a) two concurrent identical ingests: one row, and the loser is told so
# --------------------------------------------------------------------------


def test_concurrent_identical_outcome_ingests_report_a_duplicate_not_a_500(race_engine) -> None:
    barrier = threading.Barrier(2)
    lost: list[str] = []
    app = _app(
        _held_sessionmaker(race_engine, lambda: barrier.wait(timeout=_WAIT), lost=lost)
    )
    seen: dict[str, tuple[int, str]] = {}
    errors: list[BaseException] = []

    def _ingest(slot: str) -> None:
        try:
            response = TestClient(app, raise_server_exceptions=False).post(
                "/v1/instrumentation/outcomes", json=_event(1)
            )
            seen[slot] = (response.status_code, response.text)
        except BaseException as exc:  # noqa: BLE001 - the assertion is on the list
            errors.append(exc)
            barrier.abort()  # never strand the peer on a caller that died

    def first() -> None:
        _ingest("first")

    def second() -> None:
        _ingest("second")

    _run(first, second)

    assert errors == [], f"a concurrent caller failed: {errors!r}"
    # Neither caller may be handed a 500 for a race the server is supposed to
    # resolve. Both raced -- the barrier is the pre-check, so both observed
    # "no such outcome" -- and exactly one of them stored the row.
    assert len(lost) == 1, f"expected exactly one caller to hit the constraint, got {lost!r}"
    assert sorted(code for code, _ in seen.values()) == [200, 200], f"seen={seen!r}"
    statuses = sorted(json.loads(body)["status"] for _, body in seen.values())
    assert statuses == ["duplicate", "inserted"], f"seen={seen!r}"
    assert _stored(race_engine) == ["case-1"]


# --------------------------------------------------------------------------
# (b) a batch that loses the race must not lose the events it did accept
# --------------------------------------------------------------------------


def test_a_batch_losing_the_race_on_one_event_still_stores_the_other_events(
    race_engine,
) -> None:
    reached_precheck = threading.Event()
    rival_committed = threading.Event()

    def hold() -> None:
        reached_precheck.set()
        assert rival_committed.wait(timeout=_WAIT), "the rival ingest never committed"

    lost: list[str] = []
    batch_app = _app(_held_sessionmaker(race_engine, hold, lost=lost))
    rival_app = _app(_plain_sessionmaker(race_engine))
    batch: dict[str, object] = {}
    errors: list[BaseException] = []

    def batch_caller() -> None:
        try:
            response = TestClient(batch_app, raise_server_exceptions=False).post(
                "/v1/outcomes/ingest",
                json={"events": [_event(index) for index in range(1, 5)]},
            )
            batch["code"] = response.status_code
            batch["body"] = response.text
        except BaseException as exc:  # noqa: BLE001 - the assertion is on the list
            errors.append(exc)
        finally:
            reached_precheck.set()

    def rival_caller() -> None:
        try:
            assert reached_precheck.wait(timeout=_WAIT), "the batch never reached its pre-check"
            response = TestClient(rival_app, raise_server_exceptions=False).post(
                "/v1/instrumentation/outcomes", json=_event(1)
            )
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "inserted", response.text
        except BaseException as exc:  # noqa: BLE001 - the assertion is on the list
            errors.append(exc)
        finally:
            rival_committed.set()

    _run(batch_caller, rival_caller)

    assert errors == [], f"a concurrent caller failed: {errors!r}"
    # The batch must not be handed a 500, and losing one event must not take the
    # events it *did* own down with it -- so the stored rows are in the message.
    assert len(lost) == 1, f"the batch never actually lost a race: {lost!r}"
    assert batch["code"] == 200, f"{batch!r} stored={_stored(race_engine)!r}"
    # The event the rival won is reported as the duplicate it is; the three the
    # batch owned outright are not collateral damage.
    assert [item["status"] for item in json.loads(batch["body"])] == [
        "duplicate",
        "inserted",
        "inserted",
        "inserted",
    ]
    assert _stored(race_engine) == ["case-1", "case-2", "case-3", "case-4"]


# --------------------------------------------------------------------------
# (c) the pre-check has to be keyed exactly the way the index is
# --------------------------------------------------------------------------


def _row(join_key: str, implementation_id: str | None) -> OutcomeEvent:
    return OutcomeEvent(
        tenant_id=_TENANT,
        join_key=join_key,
        execution_id=join_key,
        capability_id="cap-a",
        implementation_id=implementation_id,
        outcome_type="conversion",
        outcome_payload_json={},
        occurred_at=_NOW,
        ingested_at=_NOW,
        outcome_timestamp=_NOW,
    )


def test_the_precheck_keys_null_and_empty_implementation_the_way_the_index_does(
    memory_engine,
) -> None:
    """``uq_outcome_events_tenant_identity`` indexes ``coalesce(implementation_id, '')``.

    So ``NULL`` and ``''`` are one key at the database.  A pre-check that branches
    to ``IS NULL`` *or* ``= value`` asks two disjoint questions instead, and an
    ingest resolving ``''`` would miss a stored ``NULL`` row and hit the constraint
    it was supposed to pre-empt.
    """
    with Session(memory_engine) as seed:
        seed.add_all([_row("case-null", None), _row("case-empty", "")])
        seed.commit()

    with Session(memory_engine) as session:
        db = ScopedSession(session, TenantWideScopeContext(tenant_id=_TENANT))

        def lookup(join_key: str, implementation_id: str | None) -> OutcomeEvent | None:
            return _existing_outcome(
                db,
                tenant_id=_TENANT,
                join_key=join_key,
                outcome_type="conversion",
                occurred_at=_NOW,
                implementation_id=implementation_id,
            )

        assert lookup("case-null", None) is not None
        assert lookup("case-empty", "") is not None
        # The two directions the disjoint pre-check gets wrong.
        assert lookup("case-null", "") is not None, "'' must find the stored NULL row"
        assert lookup("case-empty", None) is not None, "NULL must find the stored '' row"
        # A real implementation is still a key of its own.
        assert lookup("case-null", "impl-a") is None
        assert lookup("case-missing", None) is None


# --------------------------------------------------------------------------
# (d) the mid-batch collision -- only PostgreSQL can express it
# --------------------------------------------------------------------------


@pytest.mark.postgres
@requires_docker
def test_a_batch_losing_the_race_midway_keeps_the_events_it_already_accepted(
    postgres_container,
) -> None:
    """Events 1..N-1 are already staged when event N collides.

    SQLite cannot reach this shape -- the batch holds the one write lock from its
    first flush -- and it is the shape the rollback boundary is actually about:
    a bare ``db.rollback()`` at the point of failure would discard events the
    batch had already accepted.  PostgreSQL also aborts the whole transaction on
    the error, so this is the only place the recovery path is exercised against a
    dialect that refuses every statement until the rollback lands.
    """
    root_url = make_url(postgres_container.get_connection_url().replace("psycopg2", "psycopg"))
    database_name = f"econ_outcome_race_{uuid4().hex[:10]}"
    admin_engine = create_engine(root_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    engine = create_engine(root_url.set(database=database_name), future=True)
    try:
        Base.metadata.create_all(engine)
        _seed_capability(engine)

        reached_precheck = threading.Event()
        rival_committed = threading.Event()

        def hold() -> None:
            reached_precheck.set()
            assert rival_committed.wait(timeout=_WAIT), "the rival ingest never committed"

        # after=3: events 1 and 2 are flushed and uncommitted, and the pre-check
        # for event 3 -- the one the rival is about to win -- has already run.
        lost: list[str] = []
        batch_app = _app(_held_sessionmaker(engine, hold, after=3, lost=lost))
        rival_app = _app(_plain_sessionmaker(engine))
        batch: dict[str, object] = {}
        errors: list[BaseException] = []

        def batch_caller() -> None:
            try:
                response = TestClient(batch_app, raise_server_exceptions=False).post(
                    "/v1/outcomes/ingest",
                    json={"events": [_event(index) for index in range(1, 5)]},
                )
                batch["code"] = response.status_code
                batch["body"] = response.text
            except BaseException as exc:  # noqa: BLE001 - the assertion is on the list
                errors.append(exc)
            finally:
                reached_precheck.set()

        def rival_caller() -> None:
            try:
                assert reached_precheck.wait(timeout=_WAIT), "the batch never reached event 3"
                response = TestClient(rival_app, raise_server_exceptions=False).post(
                    "/v1/instrumentation/outcomes", json=_event(3)
                )
                assert response.status_code == 200, response.text
                assert response.json()["status"] == "inserted", response.text
            except BaseException as exc:  # noqa: BLE001 - the assertion is on the list
                errors.append(exc)
            finally:
                rival_committed.set()

        _run(batch_caller, rival_caller)

        assert errors == [], f"a concurrent caller failed: {errors!r}"
        assert len(lost) == 1, f"the batch never actually lost a race: {lost!r}"
        assert batch["code"] == 200, f"{batch!r} stored={_stored(engine)!r}"
        assert [item["status"] for item in json.loads(batch["body"])] == [
            "inserted",
            "inserted",
            "duplicate",
            "inserted",
        ]
        assert _stored(engine) == ["case-1", "case-2", "case-3", "case-4"]
    finally:
        engine.dispose()
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
        admin_engine.dispose()


# --------------------------------------------------------------------------
# (e) the database where the identity migration refused: no index at all
# --------------------------------------------------------------------------


@pytest.fixture
def unindexed_engine(tmp_path):
    """The state ``20260812_07`` leaves a database in when it refuses.

    ``outcome_events`` is under right-to-erasure audit, so the migration declines
    rather than deleting the rows that collide -- and a database that already
    held duplicate identities therefore carries *no* identity index at all.
    """
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'econ-outcome-unindexed.db'}",
        future=True,
        connect_args={"timeout": _WAIT, "check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP INDEX uq_outcome_events_tenant_identity"))
    _seed_capability(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def test_without_the_identity_index_the_precheck_is_all_there_is(unindexed_engine) -> None:
    """Pins exactly how much of this fix survives without the constraint behind it.

    The recovery path is inert here -- no constraint, so no ``IntegrityError`` to
    recover from -- and there was never a 500 to fix either, for the same reason.
    A sequential retry still resolves through the pre-check, and a concurrent one
    still stores twice, exactly as before this change.  Only the operator
    resolving the duplicates and re-running the migration closes that.
    """
    sequential = TestClient(_app(_plain_sessionmaker(unindexed_engine)))
    assert sequential.post("/v1/instrumentation/outcomes", json=_event(1)).json() == {
        "status": "inserted",
        "execution_id": "case-1",
    }
    assert sequential.post("/v1/instrumentation/outcomes", json=_event(1)).json()["status"] == (
        "duplicate"
    )

    barrier = threading.Barrier(2)
    racing = _app(_held_sessionmaker(unindexed_engine, lambda: barrier.wait(timeout=_WAIT)))
    seen: dict[str, tuple[int, str]] = {}
    errors: list[BaseException] = []

    def _ingest(slot: str) -> None:
        try:
            response = TestClient(racing, raise_server_exceptions=False).post(
                "/v1/instrumentation/outcomes", json=_event(2)
            )
            seen[slot] = (response.status_code, response.text)
        except BaseException as exc:  # noqa: BLE001 - the assertion is on the list
            errors.append(exc)
            barrier.abort()

    def first() -> None:
        _ingest("first")

    def second() -> None:
        _ingest("second")

    _run(first, second)

    assert errors == [], f"a concurrent caller failed: {errors!r}"
    assert sorted(code for code, _ in seen.values()) == [200, 200], f"seen={seen!r}"
    # Both stored: with no constraint the pre-check window is simply open, which
    # is what it was before this change too.
    assert sorted(json.loads(body)["status"] for _, body in seen.values()) == [
        "inserted",
        "inserted",
    ]
    assert _stored(unindexed_engine) == ["case-1", "case-2", "case-2"]


# --------------------------------------------------------------------------
# (f) the last resort: a conflict the replay could not resolve
# --------------------------------------------------------------------------


def test_a_batch_that_runs_out_of_replays_is_a_409_not_a_500(race_engine, monkeypatch) -> None:
    """Exhausting ``_BATCH_IDENTITY_ATTEMPTS`` must still not surface as a 500.

    Pinned by squeezing the replay budget to one attempt, which is the same state
    a batch reaches when a third writer collides inside the replay.  409 and not
    422: the payload is not what is wrong, and telling a caller its request was
    invalid when the server lost a race sends it to fix the wrong thing.
    """
    monkeypatch.setattr(service, "_BATCH_IDENTITY_ATTEMPTS", 1)
    reached_precheck = threading.Event()
    rival_committed = threading.Event()

    def hold() -> None:
        reached_precheck.set()
        assert rival_committed.wait(timeout=_WAIT), "the rival ingest never committed"

    batch_app = _app(_held_sessionmaker(race_engine, hold))
    rival_app = _app(_plain_sessionmaker(race_engine))
    batch: dict[str, object] = {}
    errors: list[BaseException] = []

    def batch_caller() -> None:
        try:
            response = TestClient(batch_app, raise_server_exceptions=False).post(
                "/v1/outcomes/ingest",
                json={"events": [_event(index) for index in range(1, 3)]},
            )
            batch["code"] = response.status_code
            batch["body"] = response.text
        except BaseException as exc:  # noqa: BLE001 - the assertion is on the list
            errors.append(exc)
        finally:
            reached_precheck.set()

    def rival_caller() -> None:
        try:
            assert reached_precheck.wait(timeout=_WAIT), "the batch never reached its pre-check"
            response = TestClient(rival_app, raise_server_exceptions=False).post(
                "/v1/instrumentation/outcomes", json=_event(1)
            )
            assert response.status_code == 200, response.text
        except BaseException as exc:  # noqa: BLE001 - the assertion is on the list
            errors.append(exc)
        finally:
            rival_committed.set()

    _run(batch_caller, rival_caller)

    assert errors == [], f"a concurrent caller failed: {errors!r}"
    assert batch["code"] == 409, batch
    assert json.loads(batch["body"])["detail"].startswith("outcome identity conflicted")
    # Still all-or-nothing: the unresolved batch left nothing of its own behind.
    assert _stored(race_engine) == ["case-1"]
