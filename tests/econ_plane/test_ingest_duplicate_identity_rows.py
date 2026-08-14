"""The identity pre-checks assume the identity resolves to at most one row.

``_existing_outcome``, ``_existing_execution`` and the ``linked_execution``
lookup on the outcome path all ended in ``scalar_one_or_none()``.  That call is a
question about cardinality, and on a database whose uniqueness guard is *absent*
while duplicate rows are already present it answers ``MultipleResultsFound`` --
which is neither a ``ValueError`` nor an ``IntegrityError``, so it escaped both
endpoints as an HTTP 500.

That database is not hypothetical for outcomes.  ``20260812_07`` refuses rather
than deleting rows out of an erasure-audited table when it finds colliding
identities (``refuses_existing_duplicates: True``, ``deletes_rows: False``), so a
database that already held duplicates converges *without*
``uq_outcome_events_tenant_identity`` and keeps serving.  The execution
constraint has no such refusal path in this repo -- the legacy shape
``20260811_05`` documents carries a *stricter* global unique on ``execution_id``,
under which duplicate ``(tenant_id, execution_id)`` rows cannot arise -- so an
``execution_events`` missing ``uq_execution_events_tenant_execution_id`` while
holding duplicates is operator-induced rather than migration-induced.  It is
covered anyway: both execution sites read that table with that key, and neither
may answer a caller with a 500 or with somebody else's payload.

The three sites need three different answers, and mirroring one onto the others
would itself be the bug:

* An outcome duplicate is duplicate *by identity key* and an outcome carries no
  immutable fields, so the colliding rows are the same logical event and picking
  one deterministically is safe.
* An execution duplicate need not be: two rows sharing an ``execution_id`` can
  differ in immutable fields, which is exactly why
  ``_resolve_existing_execution`` exists.  Picking one arbitrarily would tell a
  caller its payload was stored when a different one was.
* The ``linked_execution`` lookup is on the *outcome* endpoint and reads three
  fields off the row it finds.  Refusing an outcome over the other nine would
  punish it for a divergence that cannot reach it.

Everything is driven at the HTTP boundary, because that is where the 500
surfaced.  There is no PostgreSQL leg: ``MultipleResultsFound`` is raised by
SQLAlchemy off the fetched result set, with no constraint, no transaction abort
and no rollback ordering in play, so nothing here is dialect-dependent.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from zeroth.econ.plane.auth.deps import get_current_scoped_db, get_current_user
from zeroth.econ.plane.auth.scoped import ScopedUserClaims
from zeroth.econ.plane.capabilities.models import Capability, Implementation
from zeroth.econ.plane.database import Base
from zeroth.econ.plane.instrumentation.models import ExecutionEvent, OutcomeEvent
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext

_NOW = datetime(2026, 8, 13, tzinfo=UTC)
_TENANT = "tenant-a"
_EXECUTION_UNIQUE = "uq_execution_events_tenant_execution_id"
_OUTCOME_INDEX = "uq_outcome_events_tenant_identity"


# --------------------------------------------------------------------------
# schema: the guards the operator's database is missing
# --------------------------------------------------------------------------


def _create_unguarded_execution_events(engine) -> None:
    """Create ``execution_events`` without its ``(tenant_id, execution_id)`` unique.

    SQLite emits a table-level UNIQUE inside ``CREATE TABLE`` and has no
    ``ALTER TABLE ... DROP CONSTRAINT``, so unlike the outcome index this one
    cannot be dropped after the fact.  The table is built first, from a copy of
    the mapped table with that one constraint discarded, and ``create_all``'s
    ``checkfirst`` then leaves it alone -- so every other column, index and table
    still comes from the model rather than from a hand-written DDL that would
    drift away from it.
    """
    metadata = MetaData()
    table = ExecutionEvent.__table__.to_metadata(metadata)
    for constraint in list(table.constraints):
        if constraint.name == _EXECUTION_UNIQUE:
            table.constraints.discard(constraint)
    metadata.create_all(engine)


def _engine(path, *, execution_unique: bool = True, outcome_index: bool = True):
    engine = create_engine(
        f"sqlite+pysqlite:///{path}",
        future=True,
        connect_args={"check_same_thread": False},
    )
    if not execution_unique:
        _create_unguarded_execution_events(engine)
    Base.metadata.create_all(engine)
    if not outcome_index:
        with engine.begin() as connection:
            connection.execute(text(f"DROP INDEX {_OUTCOME_INDEX}"))
    with Session(engine) as seed:
        seed.add_all(
            [
                Capability(id="cap-a", tenant_id=_TENANT, name="A"),
                Implementation(
                    id="impl-a", tenant_id=_TENANT, capability_id="cap-a", name="A one"
                ),
                Implementation(
                    id="impl-b", tenant_id=_TENANT, capability_id="cap-a", name="A two"
                ),
            ]
        )
        seed.commit()
    return engine


@pytest.fixture
def unguarded_outcomes(tmp_path):
    """No ``uq_outcome_events_tenant_identity`` -- the state ``20260812_07`` leaves."""
    engine = _engine(tmp_path / "econ-dup-outcomes.db", outcome_index=False)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def unguarded_executions(tmp_path):
    """No ``uq_execution_events_tenant_execution_id``; the outcome index is intact.

    Keeping the outcome index means a failure on the outcome endpoint here can
    only have come from the ``linked_execution`` lookup, never from
    ``_existing_outcome``.
    """
    engine = _engine(tmp_path / "econ-dup-executions.db", execution_unique=False)
    try:
        yield engine
    finally:
        engine.dispose()


# --------------------------------------------------------------------------
# app + payloads
# --------------------------------------------------------------------------


def _app(engine) -> FastAPI:
    from zeroth.econ.plane.instrumentation.api import router as instrumentation_router

    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False, class_=Session)
    app = FastAPI()
    app.include_router(instrumentation_router, prefix="/v1")

    def scoped_db():
        with factory() as db:
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


def _client(engine) -> TestClient:
    return TestClient(_app(engine), raise_server_exceptions=False)


def _execution_payload(execution_id: str, *, model_version: str = "v1") -> dict:
    return {
        "execution_id": execution_id,
        "timestamp": _NOW.isoformat(),
        "capability_id": "cap-a",
        "implementation_id": "impl-a",
        "model_version": model_version,
        "latency_ms": 10,
        "compute_time_ms": 5,
    }


def _outcome_payload(join_key: str, *, execution_id: str | None = None) -> dict:
    body: dict = {
        "capability_id": "cap-a",
        "outcome_type": "conversion",
        "outcome_value": True,
        "occurred_at": _NOW.isoformat(),
    }
    if execution_id is None:
        body["join_key"] = join_key
        body["implementation_id"] = "impl-a"
    else:
        body["execution_id"] = execution_id
    return body


# --------------------------------------------------------------------------
# seeding: the colliding rows the missing guard let in
# --------------------------------------------------------------------------


def _execution_row(
    execution_id: str,
    *,
    model_version: str = "v1",
    join_key: str | None = None,
    implementation_id: str = "impl-a",
) -> ExecutionEvent:
    """A row identical to what ``_execution_payload`` would have stored.

    The defaults matter: ``ingest_execution`` derives ``join_key`` from the
    ``execution_id``, leaves the three cost columns NULL under an unmeasured
    payload and stamps the bound tenant into the metadata.  A seed that differed
    on any of those would be *conflicting* rather than duplicate, and the test
    would pass for the wrong reason.
    """
    return ExecutionEvent(
        tenant_id=_TENANT,
        execution_id=execution_id,
        join_key=join_key if join_key is not None else execution_id,
        timestamp=_NOW.replace(tzinfo=None),
        capability_id="cap-a",
        implementation_id=implementation_id,
        model_version=model_version,
        latency_ms=10,
        compute_time_ms=5,
        event_metadata={"tenant_id": _TENANT},
    )


def _outcome_row(join_key: str) -> OutcomeEvent:
    return OutcomeEvent(
        tenant_id=_TENANT,
        join_key=join_key,
        execution_id=join_key,
        capability_id="cap-a",
        implementation_id="impl-a",
        outcome_type="conversion",
        outcome_payload_json={},
        occurred_at=_NOW,
        ingested_at=_NOW,
        outcome_timestamp=_NOW,
    )


def _seed(engine, *rows) -> None:
    with Session(engine) as seed:
        seed.add_all(rows)
        seed.commit()


def _stored_outcomes(engine) -> list[str]:
    with Session(engine) as read:
        return sorted(read.scalars(select(OutcomeEvent.join_key)).all())


def _stored_executions(engine) -> list[tuple[str, str]]:
    with Session(engine) as read:
        return sorted(
            read.execute(
                select(ExecutionEvent.execution_id, ExecutionEvent.model_version)
            ).all()
        )


# --------------------------------------------------------------------------
# (a) the outcome identity: same logical event, so pick one deterministically
# --------------------------------------------------------------------------


def test_duplicate_outcome_rows_report_a_duplicate_not_a_500(unguarded_outcomes) -> None:
    """Colliding outcome rows are the same logical event, and are reported as one.

    They agree on every column of the identity by construction -- that is what
    "colliding" means -- and an outcome carries no immutable fields for them to
    disagree about, so there is no payload the caller could have sent that one of
    them answers differently from the other.  Reporting a duplicate is therefore
    the whole truth, and which row backs the report cannot change it.
    """
    _seed(unguarded_outcomes, _outcome_row("case-dup"), _outcome_row("case-dup"))

    response = _client(unguarded_outcomes).post(
        "/v1/instrumentation/outcomes", json=_outcome_payload("case-dup")
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "duplicate", "execution_id": "case-dup"}
    # Reported, not stored again: a duplicate must not become a third row.
    assert _stored_outcomes(unguarded_outcomes) == ["case-dup", "case-dup"]


def test_the_reported_outcome_duplicate_does_not_move_as_more_duplicates_land(
    unguarded_outcomes,
) -> None:
    """The pick is by ascending ``id``, and that choice is the point.

    ``.desc()`` -- the plane's idiom for genuinely versioned records like
    ``latest_cost_estimate`` -- would answer with a different row every time
    another duplicate landed.  Ascending ``id`` answers with the first stored
    row, which is what a sequential retry was told before the duplicates existed,
    and it keeps answering that no matter how many more arrive.
    """
    _seed(unguarded_outcomes, _outcome_row("case-stable"), _outcome_row("case-stable"))
    client = _client(unguarded_outcomes)
    first = client.post(
        "/v1/instrumentation/outcomes", json=_outcome_payload("case-stable")
    )
    assert first.status_code == 200, first.text

    with Session(unguarded_outcomes) as read:
        earliest = read.scalars(
            select(OutcomeEvent.id).where(OutcomeEvent.join_key == "case-stable")
        ).first()

    _seed(unguarded_outcomes, _outcome_row("case-stable"))
    second = client.post(
        "/v1/instrumentation/outcomes", json=_outcome_payload("case-stable")
    )

    assert second.status_code == 200, second.text
    assert second.json() == first.json()
    with Session(unguarded_outcomes) as read:
        still_earliest = read.scalars(
            select(OutcomeEvent.id).where(OutcomeEvent.join_key == "case-stable")
        ).first()
    assert still_earliest == earliest


# --------------------------------------------------------------------------
# (b) the execution identity: the stored rows may not be the same execution
# --------------------------------------------------------------------------


def test_duplicate_identical_execution_rows_report_a_duplicate_not_a_500(
    unguarded_executions,
) -> None:
    """Two stored rows that agree on every immutable field answer as one row.

    Nothing is ambiguous here -- whichever row the resolution reads, the answer
    to "is this payload what is stored?" is the same -- so the caller gets the
    duplicate a sequential retry has always been given.
    """
    _seed(unguarded_executions, _execution_row("exec-dup"), _execution_row("exec-dup"))

    response = _client(unguarded_executions).post(
        "/v1/instrumentation/executions", json=_execution_payload("exec-dup")
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "duplicate", "execution_id": "exec-dup"}
    assert _stored_executions(unguarded_executions) == [
        ("exec-dup", "v1"),
        ("exec-dup", "v1"),
    ]


def test_a_divergent_payload_against_duplicate_execution_rows_is_a_422(
    unguarded_executions,
) -> None:
    """The stored rows agree with each other and disagree with the payload.

    That is the ordinary conflict ``_resolve_existing_execution`` already names,
    and the duplication must not launder it into a ``"duplicate"``: the caller's
    ``v9`` was not stored.
    """
    _seed(unguarded_executions, _execution_row("exec-conflict"), _execution_row("exec-conflict"))

    response = _client(unguarded_executions).post(
        "/v1/instrumentation/executions",
        json=_execution_payload("exec-conflict", model_version="v9"),
    )

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail.startswith("execution_id already exists with conflicting immutable fields")
    assert "model_version" in detail, detail
    assert _stored_executions(unguarded_executions) == [
        ("exec-conflict", "v1"),
        ("exec-conflict", "v1"),
    ]


def test_execution_rows_that_disagree_with_each_other_are_a_422_not_a_pick(
    unguarded_executions,
) -> None:
    """Two rows under one ``execution_id`` recording *different* executions.

    Picking either one would make the endpoint's answer depend on which row it
    read: against the ``v1`` row the payload is a duplicate, against the ``v2``
    row it is a conflict.  Neither answer is defensible, because the identity
    that is supposed to name one execution names two contradictory ones -- so the
    honest reply is to name the fields they disagree on and refuse, which is the
    same 422 shape a conflicting payload gets and is equally actionable.
    """
    _seed(
        unguarded_executions,
        _execution_row("exec-split", model_version="v1"),
        _execution_row("exec-split", model_version="v2"),
    )

    response = _client(unguarded_executions).post(
        "/v1/instrumentation/executions", json=_execution_payload("exec-split")
    )

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail == (
        "execution_id resolves to multiple stored executions that disagree on "
        "immutable fields: model_version"
    ), detail
    # The refusal is a refusal: the payload was not quietly stored as a third row.
    assert _stored_executions(unguarded_executions) == [
        ("exec-split", "v1"),
        ("exec-split", "v2"),
    ]


# --------------------------------------------------------------------------
# (c) the linked execution on the outcome path: judged on what it is read for
# --------------------------------------------------------------------------


def test_an_outcome_linking_to_agreeing_duplicate_executions_is_ingested(
    unguarded_executions,
) -> None:
    """The outcome reads three fields off the linked row, and both rows agree.

    They differ on ``model_version``, which the outcome path never looks at:
    ``_assert_outcome_consistent`` cross-checks ``join_key`` and
    ``capability_id``, and ``implementation_id`` is derived from the row when the
    payload omits it.  Refusing over a divergence that cannot reach the outcome
    would punish the caller for somebody else's data.
    """
    _seed(
        unguarded_executions,
        _execution_row("exec-linked", model_version="v1"),
        _execution_row("exec-linked", model_version="v2"),
    )

    response = _client(unguarded_executions).post(
        "/v1/instrumentation/outcomes",
        json=_outcome_payload("exec-linked", execution_id="exec-linked"),
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "inserted", "execution_id": "exec-linked"}
    assert _stored_outcomes(unguarded_executions) == ["exec-linked"]


def test_an_outcome_linking_to_disagreeing_executions_is_a_422_not_a_500(
    unguarded_executions,
) -> None:
    """``implementation_id`` is *derived* from the linked row, so a pick is an answer.

    The payload names no implementation, so whichever row the lookup returned
    would decide which implementation the outcome is attributed to -- and
    ``implementation_id`` is part of the outcome's own identity key, so the pick
    would also decide which outcome this event is a duplicate of.  There is no
    defensible arbitrary choice; naming the disagreement is the only honest reply.
    """
    _seed(
        unguarded_executions,
        _execution_row("exec-forked", implementation_id="impl-a"),
        _execution_row("exec-forked", implementation_id="impl-b"),
    )

    response = _client(unguarded_executions).post(
        "/v1/instrumentation/outcomes",
        json=_outcome_payload("exec-forked", execution_id="exec-forked"),
    )

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    # Its own message, not the execution endpoint's: the basis of the refusal is
    # the three fields an outcome links by, and the detail says so.
    assert detail == (
        "execution_id resolves to multiple stored executions that disagree on "
        "the fields an outcome links by: implementation_id"
    ), detail
    assert _stored_outcomes(unguarded_executions) == []


def test_an_outcome_linking_to_a_forked_join_key_is_a_422(unguarded_executions) -> None:
    """``join_key`` is derived the same way, and it *is* the outcome's identity.

    A pick here would silently decide which join key the outcome is filed under,
    which is the column every downstream cost/outcome join reads.
    """
    _seed(
        unguarded_executions,
        _execution_row("exec-keys", join_key="run-1"),
        _execution_row("exec-keys", join_key="run-2"),
    )

    response = _client(unguarded_executions).post(
        "/v1/instrumentation/outcomes",
        json=_outcome_payload("exec-keys", execution_id="exec-keys"),
    )

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail == (
        "execution_id resolves to multiple stored executions that disagree on "
        "the fields an outcome links by: join_key"
    ), detail
    assert _stored_outcomes(unguarded_executions) == []
