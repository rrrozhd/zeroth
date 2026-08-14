"""The execution identity pre-check is a pre-check, not the guard.

Sibling of ``test_outcome_ingest_race``.  ``ingest_execution`` looks up
``(tenant_id, execution_id)`` and, finding nothing, builds a row and writes it.
Between that SELECT and the write there is no lock and no ``ON CONFLICT``, so two
concurrent identical ingests both miss the lookup and the loser's write raises
``IntegrityError`` against ``uq_execution_events_tenant_execution_id``.  That is
not a ``ValueError``, and ``post_execution`` catches only ``ValueError`` -- so the
loser got an HTTP 500 for a race the server is supposed to resolve.

Two things differ from the outcome shape and are pinned here rather than
mirrored blindly:

* There is no execution batch endpoint -- ``/instrumentation/executions`` takes
  one event -- so there is no batch rollback boundary and no mid-batch collision
  to reach.  Every race below is therefore expressible on SQLite; the PostgreSQL
  test at the bottom is not about a shape SQLite cannot reach but about a
  *dialect* behaviour: Postgres aborts the whole transaction on the error, so the
  post-rollback re-query and conflict re-check have to work against a connection
  that refuses every statement until the rollback lands.

* An execution has immutable fields.  Where an outcome's recovered winner is
  always the duplicate, a recovered execution may be a *different* row under the
  same ``execution_id``, and the caller must be told that -- 422 with the
  conflicting fields, exactly what the sequential retry is told -- not
  ``"duplicate"``, which would claim its payload was stored when it was not.

Every race is driven at the HTTP boundary, because that is where the defect
surfaces.
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

from tests.conftest import requires_docker
from zeroth.econ.plane.auth.deps import get_current_scoped_db, get_current_user
from zeroth.econ.plane.auth.scoped import ScopedUserClaims
from zeroth.econ.plane.capabilities.models import Capability, Implementation
from zeroth.econ.plane.database import Base
from zeroth.econ.plane.instrumentation import service
from zeroth.econ.plane.instrumentation.api import router as instrumentation_router
from zeroth.econ.plane.instrumentation.models import ExecutionEvent
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext

_NOW = datetime(2026, 8, 13, tzinfo=UTC)
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


def _event(execution_id: str, *, model_version: str = "v1") -> dict:
    return {
        "execution_id": execution_id,
        "timestamp": _NOW.isoformat(),
        "capability_id": "cap-a",
        "implementation_id": "impl-a",
        "model_version": model_version,
        "latency_ms": 10,
        "compute_time_ms": 5,
    }


def _stored(engine) -> list[tuple[str, str]]:
    with Session(engine) as read:
        return sorted(
            read.execute(
                select(ExecutionEvent.execution_id, ExecutionEvent.model_version)
            ).all()
        )


def _plain_sessionmaker(engine):
    return sessionmaker(bind=engine, autocommit=False, autoflush=False, class_=Session)


def _held_sessionmaker(engine, hold, *, after: int = 1, lost: list[str] | None = None):
    """Sessions that run ``hold()`` once, just before their ``after``-th flush.

    The identity pre-check for the event has already run by the time ``hold`` is
    called, so a caller parked here has genuinely observed "no such execution" --
    the precondition of the race.  Hooking ``flush`` catches the write on both
    sides of the fix: before it, the only flush is the implicit one inside
    ``Session.commit``; after it, the explicit one that stages the row.

    ``lost`` collects the callers whose write actually hit the constraint.  A test
    that only asserted the reported statuses would pass just as well if the two
    callers had quietly serialized and resolved through the ordinary pre-check,
    never racing at all -- so the races here measure the collision instead of
    inferring it from the harness.
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


def _race(app, body: dict, seen: dict, errors: list, barrier: threading.Barrier | None = None):
    def ingest(slot: str) -> None:
        try:
            response = TestClient(app, raise_server_exceptions=False).post(
                "/v1/instrumentation/executions", json=body
            )
            seen[slot] = (response.status_code, response.text)
        except BaseException as exc:  # noqa: BLE001 - the assertion is on the list
            errors.append(exc)
            if barrier is not None:
                barrier.abort()  # never strand the peer on a caller that died

    return ingest


def _sqlite_engine(path):
    engine = create_engine(
        f"sqlite+pysqlite:///{path}",
        future=True,
        connect_args={"timeout": _WAIT, "check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    _seed_capability(engine)
    return engine


@pytest.fixture
def race_engine(tmp_path):
    """A file-backed database with a busy timeout: two genuinely separate connections.

    The shared in-memory ``StaticPool`` fixtures elsewhere in this suite hand every
    caller the *same* connection and so cannot express concurrency at all.
    """
    engine = _sqlite_engine(tmp_path / "econ-execution-race.db")
    try:
        yield engine
    finally:
        engine.dispose()


# --------------------------------------------------------------------------
# (a) two concurrent identical ingests: one row, and the loser is told so
# --------------------------------------------------------------------------


def test_concurrent_identical_execution_ingests_report_a_duplicate_not_a_500(
    race_engine,
) -> None:
    barrier = threading.Barrier(2)
    lost: list[str] = []
    app = _app(_held_sessionmaker(race_engine, lambda: barrier.wait(timeout=_WAIT), lost=lost))
    seen: dict[str, tuple[int, str]] = {}
    errors: list[BaseException] = []
    ingest = _race(app, _event("exec-1"), seen, errors, barrier)

    def first() -> None:
        ingest("first")

    def second() -> None:
        ingest("second")

    _run(first, second)

    assert errors == [], f"a concurrent caller failed: {errors!r}"
    # Both raced -- the barrier sits after the pre-check, so both observed "no
    # such execution" -- and exactly one of them stored the row.  Neither may be
    # handed a 500 for a race the server is supposed to resolve.
    assert len(lost) == 1, f"expected exactly one caller to hit the constraint, got {lost!r}"
    assert sorted(code for code, _ in seen.values()) == [200, 200], f"seen={seen!r}"
    statuses = sorted(json.loads(body)["status"] for _, body in seen.values())
    assert statuses == ["duplicate", "inserted"], f"seen={seen!r}"
    assert _stored(race_engine) == [("exec-1", "v1")]


# --------------------------------------------------------------------------
# (b) losing to a *different* payload is the conflict it always was, not a
#     duplicate and not a 500
# --------------------------------------------------------------------------


def _assert_divergent_race(engine, seen: dict, errors: list, lost: list) -> None:
    assert errors == [], f"a concurrent caller failed: {errors!r}"
    assert len(lost) == 1, f"expected exactly one caller to hit the constraint, got {lost!r}"
    codes = sorted(code for code, _ in seen.values())
    assert codes == [200, 422], f"seen={seen!r}"
    winner = next(body for code, body in seen.values() if code == 200)
    loser = next(body for code, body in seen.values() if code == 422)
    assert json.loads(winner)["status"] == "inserted", winner
    # Same answer the sequential retry gets: the payload really is in conflict
    # with what is stored, and naming the field is the point of the message.
    detail = json.loads(loser)["detail"]
    assert detail.startswith("execution_id already exists with conflicting immutable fields")
    assert "model_version" in detail, detail
    assert len(_stored(engine)) == 1, _stored(engine)


def test_a_concurrent_ingest_that_loses_to_a_different_payload_is_a_422_not_a_500(
    race_engine,
) -> None:
    """The loser must not be told ``"duplicate"``.

    Its payload was *not* stored -- a different one under the same
    ``execution_id`` was -- and reporting a duplicate would tell the caller its
    write landed.  Recovering the winner therefore has to re-run exactly the
    comparison the pre-check runs, not assume the winner is equivalent.
    """
    barrier = threading.Barrier(2)
    lost: list[str] = []
    factory = _held_sessionmaker(race_engine, lambda: barrier.wait(timeout=_WAIT), lost=lost)
    seen: dict[str, tuple[int, str]] = {}
    errors: list[BaseException] = []
    first_ingest = _race(_app(factory), _event("exec-2", model_version="v1"), seen, errors, barrier)
    second_ingest = _race(
        _app(factory), _event("exec-2", model_version="v2"), seen, errors, barrier
    )

    def first() -> None:
        first_ingest("first")

    def second() -> None:
        second_ingest("second")

    _run(first, second)

    _assert_divergent_race(race_engine, seen, errors, lost)


# --------------------------------------------------------------------------
# (c) the last resort: a conflict the re-query cannot resolve
# --------------------------------------------------------------------------


def _seed_execution(engine, execution_id: str) -> None:
    with Session(engine) as seed:
        seed.add(
            ExecutionEvent(
                tenant_id=_TENANT,
                execution_id=execution_id,
                join_key=execution_id,
                timestamp=_NOW.replace(tzinfo=None),
                capability_id="cap-a",
                implementation_id="impl-a",
                model_version="v1",
                event_metadata={"tenant_id": _TENANT},
            )
        )
        seed.commit()


def test_an_identity_conflict_the_requery_cannot_resolve_is_a_409_not_a_500(
    race_engine, monkeypatch
) -> None:
    """A winner that took the identity and then vanished is still not a 500.

    Reporting a duplicate we cannot produce would be a worse answer than the
    conflict itself, so the recovery re-raises -- and the endpoint turns that into
    a 409.  Not a 422: the payload is not what is wrong, and telling a caller its
    request was invalid when the server lost a race sends it to fix the wrong
    thing.  Pinned by blinding the identity lookup, which is the same state the
    endpoint reaches when the winner rolls back between the failed write and the
    re-query.
    """
    _seed_execution(race_engine, "exec-ghost")
    monkeypatch.setattr(service, "_existing_execution", lambda *args, **kwargs: None)

    client = TestClient(_app(_plain_sessionmaker(race_engine)), raise_server_exceptions=False)
    response = client.post("/v1/instrumentation/executions", json=_event("exec-ghost"))

    assert response.status_code == 409, response.text
    assert response.json()["detail"].startswith("execution identity conflicted")
    assert _stored(race_engine) == [("exec-ghost", "v1")]


# --------------------------------------------------------------------------
# (d) the same recovery against a dialect that aborts the transaction
# --------------------------------------------------------------------------


@pytest.mark.postgres
@requires_docker
def test_the_conflict_recheck_runs_after_a_postgres_transaction_abort(
    postgres_container,
) -> None:
    """PostgreSQL refuses every statement until the rollback lands.

    The recovery does more than the outcome one: after rolling back it re-queries
    *and* compares twelve immutable fields.  Both of those are statements issued
    on a connection whose transaction the constraint violation aborted, so this
    pins that the rollback really does precede them.  SQLite never enters that
    state and so cannot catch an ordering mistake here.
    """
    root_url = make_url(postgres_container.get_connection_url().replace("psycopg2", "psycopg"))
    database_name = f"econ_execution_race_{uuid4().hex[:10]}"
    admin_engine = create_engine(root_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    engine = create_engine(root_url.set(database=database_name), future=True)
    try:
        Base.metadata.create_all(engine)
        _seed_capability(engine)

        barrier = threading.Barrier(2)
        lost: list[str] = []
        factory = _held_sessionmaker(engine, lambda: barrier.wait(timeout=_WAIT), lost=lost)
        seen: dict[str, tuple[int, str]] = {}
        errors: list[BaseException] = []
        first_ingest = _race(
            _app(factory), _event("exec-pg", model_version="v1"), seen, errors, barrier
        )
        second_ingest = _race(
            _app(factory), _event("exec-pg", model_version="v2"), seen, errors, barrier
        )

        def first() -> None:
            first_ingest("first")

        def second() -> None:
            second_ingest("second")

        _run(first, second)

        _assert_divergent_race(engine, seen, errors, lost)
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
