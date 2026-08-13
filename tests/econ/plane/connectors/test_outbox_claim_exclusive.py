"""ZER-49 A01-12 / AC4 -- the econ outbox claim must be exclusive.

``process_outbox_batch`` flips every selected row to ``PROCESSING`` inside a
single transaction that only commits after the whole batch has been sent, so a
second worker starting mid-batch used to see the *entire* batch as claimable and
deliver every event twice.

The Postgres test is the load-bearing one: on SQLite SQLAlchemy compiles
``FOR UPDATE SKIP LOCKED`` silently away, so a SQLite-only exclusivity guard
would pass against the unfixed code.  The SQLite tests here cover the other half
of the fix -- the conditional ``UPDATE ... RETURNING`` claim that has to hold the
line on the backend where row locking does not exist.
"""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from tests.conftest import requires_docker
from zeroth.econ.plane.connectors import service
from zeroth.econ.plane.connectors.models import (
    ConnectorConfig,
    ConnectorDeliveryLog,
    ConnectorOutbox,
)
from zeroth.econ.plane.connectors.schemas import ConnectorSendResult
from zeroth.econ.plane.database import Base
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext

TENANT = "claim-tenant"
CONNECTOR_TYPE = "posthog"
ROWS = 3
# Far enough in the past that the naive/aware skew between ``_utcnow()`` and the
# naive DateTime columns cannot make a seeded row look not-yet-due.
_PAST = datetime(2020, 1, 1, 0, 0, 0)


class _StubAdapter:
    """Adapter that always succeeds and counts the sends it was asked for."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.sends: list[str] = []

    def connector_type(self) -> str:
        return CONNECTOR_TYPE

    def validate_config(self, config: dict[str, Any]) -> None:
        return None

    def send(
        self,
        event_type: str,
        payload: dict[str, Any],
        config: dict[str, Any],
    ) -> ConnectorSendResult:
        with self.lock:
            self.sends.append(str(payload.get("event_key", event_type)))
        return ConnectorSendResult(success=True, status_code=200, response_excerpt="ok")


def _seed(engine, *, rows: int = ROWS) -> list[int]:
    """Insert one enabled connector and ``rows`` claimable outbox events."""
    with Session(engine) as session:
        session.add(
            ConnectorConfig(
                tenant_id=TENANT,
                connector_type=CONNECTOR_TYPE,
                enabled=True,
                config_json={"endpoint": "https://example.invalid/hook"},
                created_at=_PAST,
                updated_at=_PAST,
            )
        )
        outbox = [
            ConnectorOutbox(
                tenant_id=TENANT,
                event_type="execution.event",
                event_key=f"key-{index}",
                payload_json={"event_key": f"key-{index}"},
                status="PENDING",
                attempts=0,
                next_attempt_at=_PAST,
                created_at=_PAST,
            )
            for index in range(rows)
        ]
        session.add_all(outbox)
        session.commit()
        return [row.id for row in outbox]


def _enable_connectors(monkeypatch: pytest.MonkeyPatch, adapter: _StubAdapter) -> None:
    monkeypatch.setattr(service.settings, "connectors_enabled", True, raising=False)
    monkeypatch.setattr(service, "_adapter_registry", lambda: {CONNECTOR_TYPE: adapter})


def _run_batch(session_factory, results: dict[str, Any], key: str, batch_size: int) -> None:
    session = session_factory()
    try:
        db = ScopedSession(session, TenantWideScopeContext(tenant_id=TENANT))
        results[key] = service.process_outbox_batch(db, batch_size=batch_size)
    except BaseException as exc:  # noqa: BLE001 - reported through the results map
        results[key] = exc
    finally:
        session.rollback()
        session.close()


def _delivery_counts(engine) -> dict[int, int]:
    with Session(engine) as session:
        return {
            outbox_id: count
            for outbox_id, count in session.execute(
                select(ConnectorDeliveryLog.outbox_id, func.count(ConnectorDeliveryLog.id))
                .group_by(ConnectorDeliveryLog.outbox_id)
                .order_by(ConnectorDeliveryLog.outbox_id)
            ).all()
        }


def _statuses(engine) -> dict[int, str]:
    with Session(engine) as session:
        return dict(
            session.execute(
                select(ConnectorOutbox.id, ConnectorOutbox.status).order_by(ConnectorOutbox.id)
            ).all()
        )


# ---------------------------------------------------------------------------
# AC4 -- real Postgres, two concurrent workers
# ---------------------------------------------------------------------------


def _econ_config(database_url: str) -> Config:
    root = Path(__file__).parents[4]
    config = Config()
    config.set_main_option("script_location", str(root / "src/zeroth/econ/plane/_migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _fresh_postgres_engine(postgres_container, monkeypatch: pytest.MonkeyPatch):
    """Migrated, empty econ database on the shared container, plus its engine.

    ``connector_outbox`` belongs to the econ Alembic tree, not the service one,
    so this runs the econ migrations rather than reusing the ``postgres_database``
    fixture.  ``econ/plane/database.py`` binds its engine at import time and the
    suite pins ``ECP_DATABASE_URL`` to SQLite before that import, so ``SessionLocal``
    can never point here -- the engine has to be self-constructed.
    """
    root_url = make_url(postgres_container.get_connection_url().replace("psycopg2", "psycopg"))
    database_name = f"econ_outbox_claim_{uuid4().hex[:10]}"
    admin_engine = create_engine(root_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    admin_engine.dispose()
    database_url = root_url.set(database=database_name).render_as_string(hide_password=False)
    monkeypatch.setenv("ECP_DATABASE_URL", database_url)
    command.upgrade(_econ_config(database_url), "head")
    engine = create_engine(database_url, future=True, pool_size=4)
    # This guard is the whole point of running AC4 on Postgres: SQLAlchemy
    # compiles ``FOR UPDATE SKIP LOCKED`` silently away on SQLite, so a SQLite
    # run of these tests would assert nothing at all about exclusivity.
    assert engine.dialect.name == "postgresql"
    return engine


def _gate_first_send(monkeypatch: pytest.MonkeyPatch) -> tuple[threading.Event, threading.Event]:
    """Hold the first worker inside its first send until the test releases it."""
    entered_send = threading.Event()
    release_send = threading.Event()
    gate_lock = threading.Lock()
    gate_used = {"value": False}
    original_attempt_send = service._attempt_send

    def gated_attempt_send(db: ScopedSession, outbox_row: ConnectorOutbox) -> None:
        with gate_lock:
            should_gate = not gate_used["value"]
            gate_used["value"] = True
        if should_gate:
            entered_send.set()
            assert release_send.wait(timeout=60), "the gated worker was never released"
        original_attempt_send(db, outbox_row)

    monkeypatch.setattr(service, "_attempt_send", gated_attempt_send)
    return entered_send, release_send


def _require_int_results(results: dict[str, Any]) -> None:
    for key, outcome in results.items():
        if isinstance(outcome, BaseException):
            raise AssertionError(f"worker {key} failed: {outcome!r}") from outcome


@pytest.mark.postgres
@requires_docker
def test_concurrent_workers_do_not_claim_the_same_outbox_rows(
    postgres_container, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two workers racing on one outbox must not both claim the same rows.

    Worker A is held inside its first send while worker B runs a full batch.
    With an exclusive claim B finds nothing to do and says so immediately; with
    the unfixed claim B re-selects A's whole batch and every event is delivered
    twice.
    """
    engine = _fresh_postgres_engine(postgres_container, monkeypatch)
    emitted: list[str] = []

    @event.listens_for(engine, "after_cursor_execute")
    def _record(conn, cursor, statement, parameters, context, executemany) -> None:
        emitted.append(statement)

    try:
        outbox_ids = _seed(engine)
        adapter = _StubAdapter()
        _enable_connectors(monkeypatch, adapter)
        entered_send, release_send = _gate_first_send(monkeypatch)

        def session_factory() -> Session:
            return Session(engine)

        results: dict[str, Any] = {}
        worker_a = threading.Thread(
            target=_run_batch, args=(session_factory, results, "a", ROWS), daemon=True
        )
        worker_a.start()
        assert entered_send.wait(timeout=60), "worker A never reached its first send"

        worker_b = threading.Thread(
            target=_run_batch, args=(session_factory, results, "b", ROWS), daemon=True
        )
        worker_b.start()
        # An exclusive claim lets B skip A's locked rows and finish at once. A
        # claim that merely blocks would leave B parked on A's row locks.
        worker_b.join(timeout=15)
        b_finished_while_a_held_the_batch = not worker_b.is_alive()

        release_send.set()
        worker_a.join(timeout=120)
        worker_b.join(timeout=120)
        assert not worker_a.is_alive(), "worker A did not finish"
        assert not worker_b.is_alive(), "worker B did not finish"

        _require_int_results(results)
        assert results["a"] == ROWS
        assert results["b"] == 0, "worker B claimed rows already claimed by worker A"
        assert b_finished_while_a_held_the_batch, (
            "worker B blocked on worker A's batch instead of skipping the claimed rows"
        )
        assert _delivery_counts(engine) == dict.fromkeys(outbox_ids, 1)
        assert _statuses(engine) == dict.fromkeys(outbox_ids, "SENT")
        assert sorted(adapter.sends) == sorted(f"key-{index}" for index in range(ROWS))
        assert any("FOR UPDATE SKIP LOCKED" in statement for statement in emitted), (
            "the claim never reached Postgres with a row lock"
        )
    finally:
        engine.dispose()


@pytest.mark.postgres
@requires_docker
def test_concurrent_workers_make_progress_on_disjoint_rows(
    postgres_container, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exclusivity must skip claimed rows, not stall the second worker.

    With twice the batch size queued, worker B should step over worker A's
    locked batch and claim the rest.  A claim that locked more coarsely -- or
    that blocked instead of skipping -- would leave B with nothing to do while
    half the queue sat idle.
    """
    engine = _fresh_postgres_engine(postgres_container, monkeypatch)
    try:
        outbox_ids = _seed(engine, rows=ROWS * 2)
        adapter = _StubAdapter()
        _enable_connectors(monkeypatch, adapter)
        entered_send, release_send = _gate_first_send(monkeypatch)

        def session_factory() -> Session:
            return Session(engine)

        results: dict[str, Any] = {}
        worker_a = threading.Thread(
            target=_run_batch, args=(session_factory, results, "a", ROWS), daemon=True
        )
        worker_a.start()
        assert entered_send.wait(timeout=60), "worker A never reached its first send"

        worker_b = threading.Thread(
            target=_run_batch, args=(session_factory, results, "b", ROWS), daemon=True
        )
        worker_b.start()
        worker_b.join(timeout=30)
        assert not worker_b.is_alive(), "worker B blocked instead of claiming the unlocked rows"

        release_send.set()
        worker_a.join(timeout=120)
        assert not worker_a.is_alive(), "worker A did not finish"
        _require_int_results(results)

        assert results["a"] == ROWS
        assert results["b"] == ROWS, "worker B found no work while half the queue was unclaimed"
        assert _delivery_counts(engine) == dict.fromkeys(outbox_ids, 1)
        assert _statuses(engine) == dict.fromkeys(outbox_ids, "SENT")
        assert sorted(adapter.sends) == sorted(f"key-{index}" for index in range(ROWS * 2))
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# SQLite -- the conditional claim is the only guard once the lock compiles away
# ---------------------------------------------------------------------------


def _sqlite_engine(path: Path):
    engine = create_engine(f"sqlite+pysqlite:///{path}", future=True)
    with engine.connect() as connection:
        # WAL keeps A's open read transaction from blocking B's claim, which is
        # what makes the interleave below deterministic rather than a lock race.
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    return engine


def test_sqlite_claim_is_conditional_when_another_worker_commits_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A claim that lost the race must claim nothing, not re-deliver the batch.

    Worker B is run to completion in the window between worker A's candidate
    select and worker A's claim write, so A's claim meets rows that are no
    longer ``PENDING``.  The conditional ``UPDATE ... RETURNING`` is what makes A
    notice; the unfixed code overwrites the status and sends everything again.
    """
    db_path = tmp_path / "econ_claim.db"
    schema_engine = _sqlite_engine(db_path)
    Base.metadata.create_all(schema_engine)
    outbox_ids = _seed(schema_engine)
    schema_engine.dispose()

    adapter = _StubAdapter()
    _enable_connectors(monkeypatch, adapter)

    engine_a = _sqlite_engine(db_path)
    engine_b = _sqlite_engine(db_path)
    verify_engine = _sqlite_engine(db_path)
    results: dict[str, Any] = {}
    interleaved = {"value": False}

    @event.listens_for(engine_a, "after_cursor_execute")
    def _interleave(conn, cursor, statement, parameters, context, executemany) -> None:
        if interleaved["value"] or "FROM connector_outbox" not in statement:
            return
        if not statement.lstrip().startswith("SELECT"):
            return
        interleaved["value"] = True
        _run_batch(lambda: Session(engine_b), results, "b", ROWS)

    try:
        _run_batch(lambda: Session(engine_a), results, "a", ROWS)
        for key in ("a", "b"):
            outcome = results.get(key)
            if isinstance(outcome, BaseException):
                raise AssertionError(f"worker {key} failed: {outcome!r}") from outcome

        assert interleaved["value"], "the interleave hook never fired"
        assert results["b"] == ROWS
        assert results["a"] == 0, "worker A re-claimed rows worker B had already committed"
        assert _delivery_counts(verify_engine) == dict.fromkeys(outbox_ids, 1)
        assert _statuses(verify_engine) == dict.fromkeys(outbox_ids, "SENT")
        assert len(adapter.sends) == ROWS
    finally:
        for engine in (engine_a, engine_b, verify_engine):
            engine.dispose()


def test_sqlite_batch_claims_only_eligible_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rewritten claim must keep the original eligibility rules."""
    db_path = tmp_path / "econ_eligible.db"
    engine = _sqlite_engine(db_path)
    Base.metadata.create_all(engine)
    outbox_ids = _seed(engine)

    future = datetime(2999, 1, 1, 0, 0, 0)
    with Session(engine) as session:
        session.add_all(
            [
                ConnectorOutbox(
                    tenant_id=TENANT,
                    event_type="execution.event",
                    event_key="already-processing",
                    payload_json={"event_key": "already-processing"},
                    status="PROCESSING",
                    attempts=1,
                    next_attempt_at=_PAST,
                    created_at=_PAST,
                ),
                ConnectorOutbox(
                    tenant_id=TENANT,
                    event_type="execution.event",
                    event_key="not-due-yet",
                    payload_json={"event_key": "not-due-yet"},
                    status="FAILED",
                    attempts=1,
                    next_attempt_at=future,
                    created_at=_PAST,
                ),
                ConnectorOutbox(
                    tenant_id="other-tenant",
                    event_type="execution.event",
                    event_key="other-tenant",
                    payload_json={"event_key": "other-tenant"},
                    status="PENDING",
                    attempts=0,
                    next_attempt_at=_PAST,
                    created_at=_PAST,
                ),
            ]
        )
        session.commit()

    adapter = _StubAdapter()
    _enable_connectors(monkeypatch, adapter)
    results: dict[str, Any] = {}
    try:
        _run_batch(lambda: Session(engine), results, "a", 50)
        outcome = results["a"]
        if isinstance(outcome, BaseException):
            raise AssertionError(f"worker failed: {outcome!r}") from outcome
        assert outcome == ROWS
        assert sorted(adapter.sends) == sorted(f"key-{index}" for index in range(ROWS))
        assert _delivery_counts(engine) == dict.fromkeys(outbox_ids, 1)
        statuses = _statuses(engine)
        for outbox_id in outbox_ids:
            assert statuses[outbox_id] == "SENT"
        assert sorted(statuses.values()) == sorted(
            ["SENT"] * ROWS + ["PROCESSING", "FAILED", "PENDING"]
        )
    finally:
        engine.dispose()
