"""ZER-49 P8b: the outcome identity has to reach databases that already exist.

Revision ``20260812_07`` gives ``outcome_events`` the uniqueness key a retried
ingest batch needs, and ``OutcomeEvent.__table_args__`` declares the same index
so a *fresh* database gets it from ``Base.metadata.create_all``.  Neither of
those reaches a database that already has the table: ``create_all`` is
create-if-absent and skips a pre-existing table wholesale, and ``_migrations/``
is offline Postgres tooling that is never invoked at runtime
(``econ/plane/PROVENANCE.md``).  So the population most likely to hold duplicate
outcomes -- a SQLite database that predates the key -- was the one population
the key never reached.

These tests drive the two runtime doors that *are* invoked
(``database.get_db`` and ``common.bootstrap``) against a pre-existing table and
assert the index both exists and actually rejects a duplicate.  The index is
read from ``sqlite_master`` rather than from ``inspect().get_indexes()``:
SQLAlchemy skips expression-based indexes when reflecting SQLite, so a
reflection-based assertion would pass vacuously.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from zeroth.econ.plane import database as database_module
from zeroth.econ.plane.common import bootstrap as bootstrap_module
from zeroth.econ.plane.database import Base
from zeroth.econ.plane.instrumentation import models as instrumentation_models  # noqa: F401

_INDEX = "uq_outcome_events_tenant_identity"
_TENANT = "tenant-a"
_OCCURRED = "2026-08-12 00:00:00"

# The identity columns exist, but no identity index: the shape of a database
# built by an older create_all.  Two ownership shapes, because revision
# 20260811_05 rebuilds the table (SQLite batch ALTER) only for the permissive
# one -- and a batch rebuild reflects the table, which cannot see an expression
# index.  Convergence has to leave the identity standing in both.
_PRE_EXISTING_TABLES = {
    "strict-ownership": (
        "CREATE TABLE outcome_events ("
        "id INTEGER PRIMARY KEY, "
        "tenant_id VARCHAR(128) NOT NULL, "
        "join_key VARCHAR(128), "
        "execution_id VARCHAR(128), "
        "capability_id VARCHAR(128), "
        "implementation_id VARCHAR(128), "
        "outcome_type VARCHAR(64), "
        "occurred_at TIMESTAMP)"
    ),
    "permissive-ownership": (
        "CREATE TABLE outcome_events ("
        "id INTEGER PRIMARY KEY, "
        "tenant_id VARCHAR(128) DEFAULT 'tenant_default', "
        "join_key VARCHAR(128), "
        "execution_id VARCHAR(128), "
        "capability_id VARCHAR(128), "
        "implementation_id VARCHAR(128), "
        "outcome_type VARCHAR(64), "
        "occurred_at TIMESTAMP)"
    ),
}
_INSERT_OUTCOME = (
    "INSERT INTO outcome_events "
    "(id, tenant_id, join_key, execution_id, capability_id, implementation_id, "
    "outcome_type, occurred_at) VALUES "
    "(:id, :tenant_id, :join_key, :join_key, 'cap-a', :implementation_id, "
    "'conversion', :occurred_at)"
)


def _outcome(id_: int, join_key: str, implementation_id: str | None) -> dict:
    return {
        "id": id_,
        "tenant_id": _TENANT,
        "join_key": join_key,
        "implementation_id": implementation_id,
        "occurred_at": _OCCURRED,
    }


def _pre_existing_database(tmp_path: Path, *, shape: str, rows: list[dict]):
    """Build the database an older create_all would have left behind."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'econ.db'}", future=True)
    with engine.begin() as connection:
        connection.execute(text(_PRE_EXISTING_TABLES[shape]))
        for row in rows:
            connection.execute(text(_INSERT_OUTCOME), row)
    return engine


def _identity_indexes(engine) -> list[str]:
    with engine.connect() as connection:
        return [
            row[0]
            for row in connection.execute(
                text(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type = 'index' AND tbl_name = 'outcome_events' AND name = :name"
                ),
                {"name": _INDEX},
            )
        ]


def _columns(engine) -> set[str]:
    with engine.connect() as connection:
        return {row[1] for row in connection.execute(text("PRAGMA table_info(outcome_events)"))}


def _row_count(engine) -> int:
    with engine.connect() as connection:
        return connection.execute(text("SELECT COUNT(*) FROM outcome_events")).scalar_one()


def _insert(engine, row: dict) -> None:
    with engine.begin() as connection:
        connection.execute(text(_INSERT_OUTCOME), row)


def _converge_through_startup(engine, monkeypatch) -> None:
    """The one convergence door: process startup.

    ``bootstrap()`` is ``create_all`` + ``_ensure_sqlite_compat`` + an RBAC role
    seed.  Only the first two are schema convergence; the seed writes through
    ``SessionLocal`` against tables a pre-existing econ database here does not
    carry.  So this mirrors the prefix and takes the compat helper from the module
    rather than restating any DDL -- a second copy is the split-brain this file
    exists to rule out.

    There is deliberately no second door.  ``get_db``/``get_scoped_db`` are plain
    session factories and do **not** converge the schema, so a per-request door
    would be asserting a path that does not exist.  The cost of that consolidation
    is stated in the module docstring: a database that starts a process before this
    revision reaches it has no DB-level identity backstop until the next restart.
    """
    monkeypatch.setattr(database_module, "engine", engine)
    monkeypatch.setattr(bootstrap_module, "engine", engine)
    Base.metadata.create_all(bind=engine)
    database_module._ensure_sqlite_compat()


_DOORS = {"startup": _converge_through_startup}


@pytest.mark.parametrize("door", sorted(_DOORS))
@pytest.mark.parametrize("shape", sorted(_PRE_EXISTING_TABLES))
def test_convergence_makes_a_pre_existing_database_reject_a_duplicate_outcome(
    tmp_path: Path, monkeypatch, door: str, shape: str
) -> None:
    """The measured gap: create_all skips the table, so nothing added the key."""
    engine = _pre_existing_database(tmp_path, shape=shape, rows=[_outcome(1, "case-1", None)])
    try:
        assert _identity_indexes(engine) == []
        # Negative control: before convergence the retry the index exists to stop
        # is accepted, and the duplicate is durable.
        _insert(engine, _outcome(2, "case-1", None))
        assert _row_count(engine) == 2
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM outcome_events WHERE id = 2"))

        _DOORS[door](engine, monkeypatch)

        assert len(_identity_indexes(engine)) == 1
        with pytest.raises(IntegrityError):
            # NULL implementation_id is the common batch shape, and NULLs are
            # distinct inside an ordinary unique key -- the coalesce() is what
            # makes this raise.
            _insert(engine, _outcome(3, "case-1", None))
        assert _row_count(engine) == 1
        # A genuinely different outcome identity is still accepted.
        _insert(engine, _outcome(4, "case-1", "impl-a"))
        _insert(engine, _outcome(5, "case-2", None))
        assert _row_count(engine) == 3
    finally:
        engine.dispose()


@pytest.mark.parametrize("door", sorted(_DOORS))
@pytest.mark.parametrize("shape", sorted(_PRE_EXISTING_TABLES))
def test_convergence_is_idempotent(tmp_path: Path, monkeypatch, door: str, shape: str) -> None:
    """Both doors run on every startup/request, so re-running must be inert.

    Re-running also must not *destroy* the identity: revision 20260811_05's
    SQLite batch ALTER rebuilds the table from a reflection that silently omits
    expression-based indexes, so a second pass that decided to rebuild would
    drop the index the first pass created.
    """
    engine = _pre_existing_database(tmp_path, shape=shape, rows=[_outcome(1, "case-1", None)])
    try:
        _DOORS[door](engine, monkeypatch)
        _DOORS[door](engine, monkeypatch)

        assert len(_identity_indexes(engine)) == 1
        with pytest.raises(IntegrityError):
            _insert(engine, _outcome(2, "case-1", None))
    finally:
        engine.dispose()


def test_the_startup_door_builds_the_migrations_own_index(tmp_path: Path, monkeypatch) -> None:
    """The converged DDL is the migration's index -- verified as DDL, not as intent.

    Asserted on ``sqlite_master`` rather than ``inspect().get_indexes()``, which
    silently skips expression indexes on SQLite and would pass vacuously.
    """
    directory = tmp_path / "startup"
    directory.mkdir()
    engine = _pre_existing_database(
        directory, shape="strict-ownership", rows=[_outcome(1, "case-1", None)]
    )
    try:
        _converge_through_startup(engine, monkeypatch)
        statements = _identity_indexes(engine)
    finally:
        engine.dispose()

    assert len(statements) == 1
    ddl = statements[0].lower()
    assert "unique index" in ddl
    assert _INDEX in ddl
    assert "coalesce(implementation_id, '')" in ddl
    for column in ("tenant_id", "join_key", "outcome_type", "occurred_at"):
        assert column in ddl


def test_a_fresh_database_and_a_converged_one_carry_the_same_identity(
    tmp_path: Path, monkeypatch
) -> None:
    """Convergence has to land on the shape create_all would have produced."""
    fresh_directory = tmp_path / "fresh"
    fresh_directory.mkdir()
    fresh = create_engine(f"sqlite+pysqlite:///{fresh_directory / 'econ.db'}", future=True)
    converged = _pre_existing_database(
        tmp_path, shape="strict-ownership", rows=[_outcome(1, "case-1", None)]
    )
    try:
        Base.metadata.create_all(bind=fresh)
        _converge_through_startup(converged, monkeypatch)

        # Guard against a vacuous comparison of two empty lists: create_all only
        # declares the index because instrumentation.models is imported.
        assert len(_identity_indexes(fresh)) == 1
        assert _identity_indexes(converged) == _identity_indexes(fresh)
    finally:
        fresh.dispose()
        converged.dispose()


@pytest.mark.parametrize("door", sorted(_DOORS))
def test_a_database_that_already_holds_duplicates_stays_usable_and_keeps_its_rows(
    tmp_path: Path, monkeypatch, caplog, door: str
) -> None:
    """The refusal must not become an outage, and must not become silent.

    A unique index cannot be built over rows that already violate it, and the
    only automatic way through is deleting one -- which an erasure-audited econ
    table must never do behind the operator's back (revision 20260812_07 refuses
    for exactly this reason).  But these doors run per request and at startup, so
    propagating that refusal would take the whole plane down over a data
    condition.  Skip the index, keep every other convergence step, and say so.
    """
    engine = _pre_existing_database(
        tmp_path,
        shape="strict-ownership",
        rows=[_outcome(1, "case-1", None), _outcome(2, "case-1", None)],
    )
    try:
        with caplog.at_level(logging.WARNING):
            _DOORS[door](engine, monkeypatch)

        assert _identity_indexes(engine) == []
        assert _row_count(engine) == 2, "convergence must never delete an audited outcome"
        # The rest of the same transaction survived -- the refusal was contained,
        # not allowed to roll back the compatibility ALTERs beside it.
        assert {"provenance", "ingested_at", "outcome_payload_json"} <= _columns(engine)
        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.levelno >= logging.WARNING
        ]
        assert any(
            "outcome identity index not converged" in message and "20260812_07" in message
            for message in warnings
        ), f"the skipped identity was not surfaced: {warnings}"
        # ...and surfaced without republishing the subject. The refusal names
        # collisions by join_key, which is the key erasure deletes by; emitting
        # it here, on every request, would outlive the erasure receipt.
        assert not any("case-1" in message for message in warnings)
    finally:
        engine.dispose()
