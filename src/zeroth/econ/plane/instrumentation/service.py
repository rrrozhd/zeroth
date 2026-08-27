from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from zeroth.econ.plane.capabilities.models import Capability, Implementation
from zeroth.econ.plane.capabilities.service import active_experiment, pick_ab_arm
from zeroth.econ.plane.config import settings
from zeroth.econ.plane.connectors.service import enqueue_connector_event
from zeroth.econ.plane.instrumentation.models import ExecutionEvent, OutcomeEvent
from zeroth.econ.plane.instrumentation.schemas import ExecutionEventCreate, OutcomeEventCreate
from zeroth.econ.plane.scoped_session import ScopedSession


def _require_exact_scoped_session(db: object) -> ScopedSession:
    if type(db) is not ScopedSession:
        raise TypeError("instrumentation persistence requires an exact ScopedSession")
    return db


def _bound_tenant(db: ScopedSession) -> str:
    if db.scope is None:
        raise ValueError("instrumentation requires a tenant-bound scope")
    return db.scope.tenant_id


def _assert_requested_tenant(requested: str | None, tenant_id: str) -> None:
    normalized = "default" if requested == "tenant_default" else requested
    if normalized is not None and normalized != tenant_id:
        raise ValueError("tenant ownership does not match the bound scope")


def _require_capability_and_implementation(
    db: ScopedSession,
    *,
    tenant_id: str,
    capability_id: str,
    implementation_id: str | None,
) -> Capability:
    capability = db.execute(
        select(Capability).where(
            Capability.tenant_id == tenant_id,
            Capability.id == capability_id,
        )
    ).scalar_one_or_none()
    if capability is None:
        raise ValueError("capability does not exist in the bound tenant")
    if implementation_id is None:
        return capability
    implementation = db.execute(
        select(Implementation).where(
            Implementation.tenant_id == tenant_id,
            Implementation.id == implementation_id,
            Implementation.capability_id == capability_id,
        )
    ).scalar_one_or_none()
    if implementation is None:
        raise ValueError("implementation does not belong to the capability in the bound tenant")
    return capability


def _ensure_capability_and_implementation(
    db: ScopedSession,
    *,
    tenant_id: str,
    capability_id: str,
    implementation_id: str | None,
) -> None:
    """Upsert the capability/implementation an execution event names, so
    platform-emitted telemetry (capability_id=node_id, implementation_id=model)
    lands instead of 422-ing on the strict existence guard.

    DB-safe because ``ExecutionEvent`` has no FK to these tables — the guard is a
    pure business rule. Uses ``flush`` (not ``commit``) to stay inside the ingest
    transaction.

    Ownership is preserved, not relaxed (A01-32): a capability/implementation id is
    matched by its GLOBAL primary key, and only a genuinely-absent id is created —
    in the bound tenant. An id that already exists but is owned by ANOTHER tenant is
    rejected with the same strict error as before, never silently rebound and never
    PK-collided. Within the bound tenant an implementation may be reused across
    capabilities (one model, many nodes — the common ``gpt-4o`` case), which the
    strict per-capability guard would wrongly 422.
    """
    # Reads are tenant-scoped (row-level security), so "absent" means absent IN the
    # bound tenant. A genuinely-new id is created here; an id already owned by
    # ANOTHER tenant is invisible to the scoped read but collides on the global
    # primary key at flush — that collision is translated back to the strict
    # ownership error (never a 500, never a silent cross-tenant rebind), preserving
    # the A01-32 isolation contract. Within the bound tenant an implementation may
    # be reused across capabilities (one model, many nodes).
    capability = db.execute(
        select(Capability).where(Capability.id == capability_id)
    ).scalar_one_or_none()
    if capability is None:
        try:
            db.add(Capability(id=capability_id, tenant_id=tenant_id, name=capability_id))
            db.flush()
        except IntegrityError as exc:
            db.rollback()
            raise ValueError("capability does not exist in the bound tenant") from exc
    if implementation_id is None:
        return
    implementation = db.execute(
        select(Implementation).where(Implementation.id == implementation_id)
    ).scalar_one_or_none()
    if implementation is None:
        try:
            db.add(
                Implementation(
                    id=implementation_id,
                    tenant_id=tenant_id,
                    capability_id=capability_id,
                    name=implementation_id,
                )
            )
            db.flush()
        except IntegrityError as exc:
            db.rollback()
            raise ValueError(
                "implementation does not belong to the capability in the bound tenant"
            ) from exc


def _derive_join_key_from_metadata(metadata: dict) -> str:
    for key in ("request_id", "trace_id", "run_id"):
        value = metadata.get(key)
        if value:
            return str(value)
    return ""


_ASSIGNMENT_METADATA_KEYS = {
    "experiment_id",
    "assigned_arm",
    "assignment_key_type",
    "assignment_input_hash",
}


def _event_metadata_identity(metadata: dict | None) -> dict:
    return {
        key: value
        for key, value in (metadata or {}).items()
        if key not in _ASSIGNMENT_METADATA_KEYS
    }


def _datetime_identity(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _execution_identity_fields(row: ExecutionEvent) -> dict:
    """The immutable fields of a *stored* execution, keyed for comparison.

    Split out so that a stored row can be compared against another stored row --
    :func:`_disagreeing_execution_fields` -- with exactly the field set and
    normalisation :func:`_execution_conflicts` uses against a payload.  Two
    comparison bases would eventually drift, and a field present in one but not
    the other would be a divergence nobody is told about.
    """
    return {
        "execution_id": row.execution_id,
        "campaign_id": row.campaign_id,
        "operation_id": row.operation_id,
        "provider_request_id": row.provider_request_id,
        "cleanup_status": row.cleanup_status,
        "capability_id": row.capability_id,
        "implementation_id": row.implementation_id,
        "join_key": row.join_key,
        "timestamp": _datetime_identity(row.timestamp),
        "model_version": row.model_version,
        "token_cost_usd": row.token_cost_usd,
        "tool_cost_usd": row.tool_cost_usd,
        "compute_cost_usd": row.compute_cost_usd,
        "latency_ms": row.latency_ms,
        "compute_time_ms": row.compute_time_ms,
        "metadata": _event_metadata_identity(row.event_metadata),
    }


def _execution_payload_fields(
    payload: ExecutionEventCreate, *, join_key: str, metadata: dict
) -> dict:
    """The same fields off an inbound payload, after the service has derived them.

    ``join_key`` and ``metadata`` are arguments rather than attributes because
    ``ingest_execution`` derives both before the row is built.
    """
    return {
        "execution_id": payload.execution_id,
        "campaign_id": payload.campaign_id,
        "operation_id": payload.operation_id,
        "provider_request_id": payload.provider_request_id,
        "cleanup_status": payload.cleanup_status,
        "capability_id": payload.capability_id,
        "implementation_id": payload.implementation_id,
        "join_key": join_key,
        "timestamp": _datetime_identity(payload.timestamp),
        "model_version": payload.model_version,
        "token_cost_usd": payload.token_cost_usd,
        "tool_cost_usd": payload.tool_cost_usd,
        "compute_cost_usd": payload.compute_cost_usd,
        "latency_ms": payload.latency_ms,
        "compute_time_ms": payload.compute_time_ms,
        "metadata": _event_metadata_identity(metadata),
    }


def _execution_conflicts(
    existing: ExecutionEvent,
    payload: ExecutionEventCreate,
    *,
    join_key: str,
    metadata: dict,
) -> list[str]:
    stored = _execution_identity_fields(existing)
    requested = _execution_payload_fields(payload, join_key=join_key, metadata=metadata)
    return [field for field, value in stored.items() if value != requested[field]]


def _disagreeing_execution_fields(rows: Sequence[ExecutionEvent]) -> list[str]:
    """Immutable fields on which the stored rows disagree *with each other*.

    Empty means every row records the same execution, so any of them answers a
    payload identically and picking one is not a choice at all.
    """
    first = _execution_identity_fields(rows[0])
    disagreements = {
        field
        for other in rows[1:]
        for field, value in _execution_identity_fields(other).items()
        if value != first[field]
    }
    return [field for field in first if field in disagreements]


def _existing_execution(
    db: ScopedSession, *, tenant_id: str, execution_id: str
) -> ExecutionEvent | None:
    """Return the already-stored execution with this identity, if any.

    The identity is exactly ``uq_execution_events_tenant_execution_id``.  Both of
    its columns are NOT NULL and the constraint keys them plainly, so this
    equality lookup asks the database's own question -- unlike the outcome
    identity there is no ``coalesce`` to reproduce and no nullable column whose
    NULL/``''`` split could make the two disagree.

    The lookup still holds no lock, so between it and the write that follows a
    concurrent caller can take the identity.  That window is closed by
    :func:`_stage_execution`, not here; what this buys is that the common case --
    a sequential retry of an ingest whose response the client never saw -- never
    reaches the constraint at all.

    The constraint is also the *only* thing that makes this identity single-valued,
    and a database can be serving without it -- an operator-modified or
    half-migrated ``execution_events`` that already held duplicates.  There
    ``scalar_one_or_none()`` raised ``MultipleResultsFound``, which is neither a
    ``ValueError`` nor an ``IntegrityError``, so it escaped ``post_execution`` as
    a 500.  Answering that with an arbitrary row would be worse than the 500: an
    execution has immutable fields, so one stored row can call a payload a
    duplicate while another calls it a conflict, and the endpoint's answer would
    turn on which row it happened to read -- the very thing
    :func:`_resolve_existing_execution` exists to prevent.  So rows that agree
    resolve as the single execution they describe, and rows that disagree are
    refused by name: the identity that should name one execution names two
    contradictory ones, and that is a fact about the data the caller can act on.
    """
    rows = list(
        db.execute(
            select(ExecutionEvent)
            .where(
                ExecutionEvent.tenant_id == tenant_id,
                ExecutionEvent.execution_id == execution_id,
            )
            .order_by(ExecutionEvent.id)
        ).scalars()
    )
    if not rows:
        return None
    disagreements = _disagreeing_execution_fields(rows)
    if disagreements:
        raise ValueError(
            "execution_id resolves to multiple stored executions that disagree on "
            "immutable fields: " + ", ".join(disagreements)
        )
    return rows[0]


def _resolve_existing_execution(
    existing: ExecutionEvent,
    payload: ExecutionEventCreate,
    *,
    join_key: str,
    metadata: dict,
) -> ExecutionEvent:
    """Answer for an ``execution_id`` already stored: the duplicate, or a conflict.

    Both the pre-check and the post-race recovery go through here, so a caller
    that *lost the race* is told exactly what a caller that arrived second
    *sequentially* is told.  That symmetry matters more here than it does for
    outcomes, which carry no immutable fields: the row that won an execution race
    may be a materially different execution under the same id, and answering
    ``"duplicate"`` would tell the caller its payload was stored when a different
    one was.
    """
    conflicts = _execution_conflicts(existing, payload, join_key=join_key, metadata=metadata)
    if conflicts:
        raise ValueError(
            "execution_id already exists with conflicting immutable fields: "
            + ", ".join(conflicts)
        )
    return existing


def _stage_execution(
    db: ScopedSession,
    row: ExecutionEvent,
    payload: ExecutionEventCreate,
    *,
    tenant_id: str,
    join_key: str,
    metadata: dict,
) -> ExecutionEvent | None:
    """Flush the new execution; return the winning row if this caller lost the race.

    ``None`` means the row was staged and this caller owns it.  Anything else is
    the row a concurrent ingest committed under the same
    ``(tenant_id, execution_id)`` between our pre-check and this flush -- the
    window :func:`_existing_execution` cannot close, because it takes no lock.
    Recovering it here is what turns the loser's ``IntegrityError`` into the
    documented answer; before this it escaped ``post_execution``, which catches
    only ``ValueError``, as a 500.

    The flush is explicit rather than left to ``commit``: ``SessionLocal`` is
    ``autoflush=False``, and deferring it would put this INSERT and the connector
    outbox INSERT in the same flush, where the error is no longer attributable to
    this row.

    Recovery is a rollback and a re-query, not a dialect-native upsert:
    ``on_conflict_do_nothing`` with index inference *raises* on a database where
    the constraint is absent, which would turn a silently-unguarded database into
    a hard-failing one, and it needs Core ``insert()``, which bypasses the
    ownership validation :class:`ScopedSession` exists to enforce.  Rolling back
    is safe because a single ingest's transaction holds nothing but this row --
    there is no execution batch endpoint, so no already-accepted events to
    discard.

    Re-raising when the re-query comes back empty is deliberate: the constraint
    fired, so *something* conflicted, and reporting a duplicate we cannot produce
    would be a worse answer than the conflict itself.

    So there are three exits, not two.  Beyond ``None`` and the winning row, the
    re-query may raise: :func:`_resolve_existing_execution` when the winner is a
    materially different execution, and :func:`_existing_execution` itself when
    the identity resolves to several stored rows that disagree with each other --
    reachable when the database has *both* a live constraint and duplicates that
    predate it.  Both are ``ValueError`` and both become the 422 that names the
    fields, which is what the sequential caller is told; only the empty re-query
    re-raises the ``IntegrityError`` for the endpoint to turn into a 409.
    """
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        winner = _existing_execution(
            db, tenant_id=tenant_id, execution_id=payload.execution_id
        )
        if winner is None:
            raise
        return _resolve_existing_execution(
            winner, payload, join_key=join_key, metadata=metadata
        )
    return None


def ingest_execution(
    db: ScopedSession, payload: ExecutionEventCreate
) -> tuple[str, ExecutionEvent]:
    db = _require_exact_scoped_session(db)
    tenant_id = _bound_tenant(db)
    metadata = dict(payload.metadata)
    _assert_requested_tenant(payload.tenant_id, tenant_id)
    metadata_tenant = metadata.get("tenant_id")
    _assert_requested_tenant(
        str(metadata_tenant) if metadata_tenant is not None else None,
        tenant_id,
    )
    metadata["tenant_id"] = tenant_id
    join_key = payload.join_key or _derive_join_key_from_metadata(metadata) or payload.execution_id
    if settings.strict_join_key_enforcement and not join_key:
        raise ValueError("join_key is required for execution ingestion")

    existing = _existing_execution(db, tenant_id=tenant_id, execution_id=payload.execution_id)
    if existing:
        return "duplicate", _resolve_existing_execution(
            existing, payload, join_key=join_key, metadata=metadata
        )

    if settings.auto_register_ingest_capabilities:
        _ensure_capability_and_implementation(
            db,
            tenant_id=tenant_id,
            capability_id=payload.capability_id,
            implementation_id=payload.implementation_id,
        )
    else:
        _require_capability_and_implementation(
            db,
            tenant_id=tenant_id,
            capability_id=payload.capability_id,
            implementation_id=payload.implementation_id,
        )

    experiment = active_experiment(db, payload.capability_id, mode="AB")
    if experiment is not None and join_key:
        assignment_input = join_key
        if experiment.assignment_key == "user_id":
            assignment_input = str(metadata.get("user_id", join_key))
        arm = pick_ab_arm(assignment_input, experiment.target_pct)
        metadata.update(
            {
                "experiment_id": experiment.id,
                "assigned_arm": arm,
                "assignment_key_type": experiment.assignment_key,
                "assignment_input_hash": assignment_input,
            }
        )

    row = ExecutionEvent(
        tenant_id=tenant_id,
        campaign_id=payload.campaign_id,
        operation_id=payload.operation_id,
        deployment_ref=payload.deployment_ref,
        evidence_kind=payload.evidence_kind,
        provider_request_id=payload.provider_request_id,
        cleanup_status=payload.cleanup_status,
        execution_id=payload.execution_id,
        join_key=join_key,
        timestamp=payload.timestamp,
        capability_id=payload.capability_id,
        implementation_id=payload.implementation_id,
        model_version=payload.model_version,
        token_cost_usd=payload.token_cost_usd,
        tool_cost_usd=payload.tool_cost_usd,
        compute_cost_usd=payload.compute_cost_usd,
        cost_measurement=payload.cost_measurement.value,
        usage_measurement=payload.usage_measurement.value,
        latency_ms=payload.latency_ms,
        compute_time_ms=payload.compute_time_ms,
        event_metadata=metadata,
    )
    winner = _stage_execution(
        db, row, payload, tenant_id=tenant_id, join_key=join_key, metadata=metadata
    )
    if winner is not None:
        return "duplicate", winner
    if settings.connectors_enabled:
        try:
            enqueue_connector_event(
                db,
                tenant_id=tenant_id,
                event_type="execution.event",
                event_key=payload.execution_id,
                join_key=join_key,
                capability_id=payload.capability_id,
                implementation_id=payload.implementation_id,
                payload={
                    "execution_id": payload.execution_id,
                    "timestamp": payload.timestamp.isoformat(),
                    "model_version": payload.model_version,
                    "token_cost_usd": str(payload.token_cost_usd),
                    "tool_cost_usd": str(payload.tool_cost_usd),
                    "compute_cost_usd": str(payload.compute_cost_usd),
                    "cost_measurement": payload.cost_measurement.value,
                    "usage_measurement": payload.usage_measurement.value,
                    "latency_ms": payload.latency_ms,
                    "compute_time_ms": payload.compute_time_ms,
                    "metadata": metadata,
                },
            )
        except Exception:  # noqa: BLE001
            # Connector path is best-effort and must not fail ingestion.
            pass
    db.commit()
    db.refresh(row)
    return "inserted", row


def _existing_outcome(
    db: ScopedSession,
    *,
    tenant_id: str,
    join_key: str,
    outcome_type: str,
    occurred_at: datetime,
    implementation_id: str | None,
) -> OutcomeEvent | None:
    """Return the already-stored outcome with this identity, if any.

    The identity matches ``uq_outcome_events_tenant_identity``.  The database
    constraint is the race-proof guard -- this lookup holds no lock, so between
    it and the flush that follows a concurrent caller can store the same
    identity.  What it buys is that the common cases never reach the constraint
    at all: a sequential retry is reported as a duplicate, and a repeat *inside
    one batch* resolves without the constraint aborting a transaction that
    carries the rest of the batch.  A caller that loses the race anyway is
    recovered by :func:`_persist_outcome` and :func:`ingest_outcomes`.

    ``implementation_id`` is keyed exactly the way the index keys it, through
    ``coalesce(implementation_id, '')``: NULL and ``''`` are one key at the
    database, so a lookup that branched to ``IS NULL`` *or* ``= value`` would ask
    two disjoint questions and let a resolved ``''`` miss a stored NULL row.

    The index is also the only thing that makes this identity single-valued, and a
    database can be serving without it: ``20260812_07`` refuses rather than
    deleting rows out of an erasure-audited table when it finds colliding
    identities, so one that already held duplicates converges *without* the index
    and keeps taking ingests.  There ``scalar_one_or_none()`` raised
    ``MultipleResultsFound`` -- neither a ``ValueError`` nor an ``IntegrityError``,
    so it escaped ``post_outcome`` as a 500.  Capping at one row answers instead,
    and unlike the execution identity that is not a choice between rival records:
    colliding rows agree on every column of the identity by construction and an
    outcome carries no immutable fields, so they are one logical event and no
    payload can be a duplicate of one and a conflict with another.

    The order is ascending ``id`` rather than the plane's ``.desc()`` idiom
    (``latest_cost_estimate`` and its siblings), which is for genuinely versioned
    records where the newest row is the answer.  Here there is no newest: the
    first-stored row is the one a sequential retry was told about before the
    duplicates accrued, and reporting it keeps the answer fixed as further
    duplicates land instead of moving with them.
    """
    return (
        db.execute(
            select(OutcomeEvent)
            .where(
                OutcomeEvent.tenant_id == tenant_id,
                OutcomeEvent.join_key == join_key,
                OutcomeEvent.outcome_type == outcome_type,
                OutcomeEvent.occurred_at == occurred_at,
                func.coalesce(OutcomeEvent.implementation_id, "") == (implementation_id or ""),
            )
            .order_by(OutcomeEvent.id)
            .limit(1)
        )
        .scalars()
        .first()
    )


#: What an outcome actually reads off the execution it claims to close.
#: ``join_key`` and ``implementation_id`` are *derived* from the linked row when
#: the payload omits them and both are part of the outcome's own identity key;
#: ``capability_id`` is cross-checked by :func:`_assert_outcome_consistent`.
#: Nothing else on the execution reaches the outcome.
_OUTCOME_LINK_FIELDS = ("join_key", "capability_id", "implementation_id")


def _linked_execution(
    db: ScopedSession, *, tenant_id: str, execution_id: str
) -> ExecutionEvent | None:
    """Resolve the execution an outcome links to, by the same identity.

    Deliberately *not* :func:`_existing_execution`, though both read
    ``execution_events`` by ``(tenant_id, execution_id)``.  That one is answering
    "is this payload already stored?", so every immutable field is in scope.  This
    one is answering "which execution is this outcome about?", and an outcome only
    ever reads :data:`_OUTCOME_LINK_FIELDS` off the row.  Rows that differ on, say,
    ``latency_ms`` describe one execution as far as any outcome is concerned, and
    refusing the ingest over that would punish the caller for a divergence that
    cannot reach it.

    Disagreement on the three that do reach it is refused, because there a pick
    would be an answer: the derived ``implementation_id`` decides which
    implementation the outcome is attributed to, ``join_key`` decides which key it
    is filed under, and both are columns of the outcome's identity -- so an
    arbitrary choice would silently decide which event this outcome is a duplicate
    of.  Before this the lookup was a bare ``scalar_one_or_none()`` and simply
    raised ``MultipleResultsFound`` here, which ``post_outcome`` surfaced as a 500.
    """
    rows = list(
        db.execute(
            select(ExecutionEvent)
            .where(
                ExecutionEvent.tenant_id == tenant_id,
                ExecutionEvent.execution_id == execution_id,
            )
            .order_by(ExecutionEvent.id)
        ).scalars()
    )
    if not rows:
        return None
    disagreements = [
        field
        for field in _OUTCOME_LINK_FIELDS
        if len({getattr(row, field) for row in rows}) > 1
    ]
    if disagreements:
        raise ValueError(
            "execution_id resolves to multiple stored executions that disagree on "
            "the fields an outcome links by: " + ", ".join(disagreements)
        )
    return rows[0]


def ingest_outcome(db: ScopedSession, payload: OutcomeEventCreate) -> OutcomeEvent:
    """Ingest one outcome and commit it. Thin wrapper over the status-aware form."""
    return ingest_outcome_with_status(db, payload)[1]


#: How many times a batch may be replayed after losing an identity race.  One
#: replay is enough for the race to be resolvable rather than merely unlikely:
#: the winner is committed by the time the loser's flush fails, so the replay's
#: pre-check sees it and reports a duplicate without reaching the constraint
#: again.  A second collision means a *third* concurrent writer arrived inside
#: the replay, and retrying forever would turn a hot key into an unbounded loop.
_BATCH_IDENTITY_ATTEMPTS = 2


def ingest_outcomes(
    db: ScopedSession, payloads: Sequence[OutcomeEventCreate]
) -> list[tuple[str, OutcomeEvent]]:
    """Ingest a batch of outcomes inside exactly one transaction.

    Every event is staged with ``commit=False`` and a single commit closes the
    batch, so a rejection on event N leaves *nothing* persisted rather than
    committing events 1..N-1 behind a 422 the caller cannot attribute (A01-38).
    The rollback is explicit: the request-scoped session outlives this call, and
    a half-built transaction left on it would leak into whatever ran next.

    Losing an identity race is the one failure that is *not* a rejection, so it
    is the one failure the batch replays instead of surfacing.  The rollback
    boundary has to be the whole batch: :class:`ScopedSession` exposes no
    ``begin_nested``, and a SAVEPOINT around each event would in any case break
    the invariant above by letting events 1..N-1 commit behind a later 422.  So
    a lost race unwinds the batch and re-runs it from the top -- the events this
    batch owned outright are re-staged, and the event the peer won now resolves
    through the pre-check as the duplicate it is.  Replaying is safe because
    nothing was committed and every event carries its own durable identity.
    """
    db = _require_exact_scoped_session(db)
    results: list[tuple[str, OutcomeEvent]] = []
    for attempt in range(_BATCH_IDENTITY_ATTEMPTS):
        results = []
        try:
            for payload in payloads:
                results.append(ingest_outcome_with_status(db, payload, commit=False))
            db.commit()
        except IntegrityError:
            db.rollback()
            if attempt + 1 == _BATCH_IDENTITY_ATTEMPTS:
                raise
            continue
        except Exception:
            db.rollback()
            raise
        break
    for _status, row in results:
        db.refresh(row)
    return results


def _assert_outcome_consistent(
    payload: OutcomeEventCreate,
    linked_execution: ExecutionEvent | None,
    join_key: str,
) -> None:
    """Cross-check an outcome payload against the execution it claims to close.

    Sits outside :func:`ingest_outcome_with_status` only to keep that function
    under the mccabe ceiling the commit gate ratchets — it is not a new
    validation seam. The four checks, their order and their exact messages are
    unchanged, and they still run before the row is built, so a rejected
    outcome writes nothing.
    """
    if settings.strict_join_key_enforcement and not join_key:
        raise ValueError("join_key is required for outcome ingestion")
    if linked_execution is not None and join_key != linked_execution.join_key:
        raise ValueError("outcome join_key does not match execution join_key")
    if linked_execution is not None and payload.capability_id != linked_execution.capability_id:
        raise ValueError("outcome capability_id does not match execution capability_id")
    if (
        linked_execution is not None
        and payload.implementation_id is not None
        and payload.implementation_id != linked_execution.implementation_id
    ):
        raise ValueError("outcome implementation_id does not match execution implementation_id")


def _stage_outcome(
    db: ScopedSession,
    row: OutcomeEvent,
    *,
    tenant_id: str,
    join_key: str,
    outcome_type: str,
    occurred_at: datetime,
    implementation_id: str | None,
    recoverable: bool,
) -> OutcomeEvent | None:
    """Flush the new outcome; return the winning row if this caller lost the race.

    ``None`` means the row was staged and this caller owns it.  Anything else is
    the row a concurrent ingest committed under the same identity between our
    pre-check and this flush -- the window ``_existing_outcome`` cannot close,
    because it takes no lock.  Recovering it here is what turns the loser's
    ``IntegrityError`` into the documented ``"duplicate"``; before this it
    escaped ``post_outcome``, which catches only ``ValueError``, as a 500.

    The flush is explicit because ``SessionLocal`` is ``autoflush=False``: without
    it the *next* event in a batch would not see this one and ``_existing_outcome``
    would miss an in-batch repeat.

    ``recoverable`` is the rollback boundary.  Recovering means rolling the
    session back, and the only caller that may do that here is the one whose
    transaction holds nothing but this event.  A batch stages many events on one
    transaction, so it passes ``recoverable=False`` and re-raises to
    :func:`ingest_outcomes`, which unwinds and replays the batch as a unit rather
    than silently discarding the events it had already accepted.

    Re-raising when the re-query comes back empty is deliberate: the constraint
    fired, so *something* conflicted, and reporting a duplicate we cannot produce
    would be a worse answer than the conflict itself.
    """
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        if not recoverable:
            raise
        db.rollback()
        winner = _existing_outcome(
            db,
            tenant_id=tenant_id,
            join_key=join_key,
            outcome_type=outcome_type,
            occurred_at=occurred_at,
            implementation_id=implementation_id,
        )
        if winner is None:
            raise
        return winner
    return None


def ingest_outcome_with_status(
    db: ScopedSession, payload: OutcomeEventCreate, *, commit: bool = True
) -> tuple[str, OutcomeEvent]:
    db = _require_exact_scoped_session(db)
    tenant_id = _bound_tenant(db)
    _assert_requested_tenant(payload.tenant_id, tenant_id)
    _require_capability_and_implementation(
        db,
        tenant_id=tenant_id,
        capability_id=payload.capability_id,
        implementation_id=None,
    )
    linked_execution = None
    if payload.execution_id:
        linked_execution = _linked_execution(
            db, tenant_id=tenant_id, execution_id=payload.execution_id
        )
        if linked_execution is None:
            raise ValueError("execution_id was not found in the bound tenant")

    join_key = (
        payload.join_key
        or (linked_execution.join_key if linked_execution else "")
        or payload.execution_id
        or ""
    )
    _assert_outcome_consistent(payload, linked_execution, join_key)
    implementation_id = payload.implementation_id or (
        linked_execution.implementation_id if linked_execution is not None else None
    )
    _require_capability_and_implementation(
        db,
        tenant_id=tenant_id,
        capability_id=payload.capability_id,
        implementation_id=implementation_id,
    )

    occurred_at = payload.occurred_at or payload.outcome_timestamp or datetime.now(UTC)
    outcome_payload = dict(payload.outcome_payload_json)
    if payload.outcome_value is not None and "value" not in outcome_payload:
        outcome_payload["value"] = payload.outcome_value

    duplicate = _existing_outcome(
        db,
        tenant_id=tenant_id,
        join_key=join_key,
        outcome_type=payload.outcome_type,
        occurred_at=occurred_at,
        implementation_id=implementation_id,
    )
    if duplicate is not None:
        return "duplicate", duplicate

    row = OutcomeEvent(
        tenant_id=tenant_id,
        join_key=join_key,
        execution_id=payload.execution_id or join_key,
        capability_id=payload.capability_id,
        implementation_id=implementation_id,
        outcome_type=payload.outcome_type,
        outcome_payload_json=outcome_payload,
        outcome_value=str(payload.outcome_value) if payload.outcome_value is not None else "",
        occurred_at=occurred_at,
        ingested_at=datetime.now(UTC),
        outcome_timestamp=payload.outcome_timestamp or occurred_at,
        provenance=payload.provenance,
    )
    winner = _stage_outcome(
        db,
        row,
        tenant_id=tenant_id,
        join_key=join_key,
        outcome_type=payload.outcome_type,
        occurred_at=occurred_at,
        implementation_id=implementation_id,
        recoverable=commit,
    )
    if winner is not None:
        return "duplicate", winner
    if settings.connectors_enabled:
        try:
            enqueue_connector_event(
                db,
                tenant_id=tenant_id,
                event_type="outcome.event",
                event_key=f"{join_key}:{occurred_at.isoformat()}:{payload.outcome_type}",
                join_key=join_key,
                capability_id=payload.capability_id,
                implementation_id=implementation_id,
                payload={
                    "execution_id": payload.execution_id,
                    "outcome_type": payload.outcome_type,
                    "outcome_value": payload.outcome_value,
                    "outcome_payload_json": outcome_payload,
                    "occurred_at": occurred_at.isoformat(),
                    "provenance": payload.provenance,
                },
            )
        except Exception:  # noqa: BLE001
            pass
    if commit:
        db.commit()
        db.refresh(row)
    return "inserted", row


def query_outcomes(
    db: ScopedSession,
    capability_id: str | None = None,
    implementation_id: str | None = None,
    outcome_type: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[OutcomeEvent]:
    db = _require_exact_scoped_session(db)
    tenant_id = _bound_tenant(db)
    stmt = select(OutcomeEvent).where(OutcomeEvent.tenant_id == tenant_id)
    if capability_id:
        stmt = stmt.where(OutcomeEvent.capability_id == capability_id)
    if implementation_id:
        stmt = stmt.where(OutcomeEvent.implementation_id == implementation_id)
    if outcome_type:
        stmt = stmt.where(OutcomeEvent.outcome_type == outcome_type)
    if start:
        stmt = stmt.where(OutcomeEvent.occurred_at >= start)
    if end:
        stmt = stmt.where(OutcomeEvent.occurred_at <= end)
    return list(db.execute(stmt.order_by(OutcomeEvent.id.desc())).scalars())
