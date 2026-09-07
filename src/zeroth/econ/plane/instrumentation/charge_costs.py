"""Append-only charge revisions and one shared effective-cost reader."""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import Integer, bindparam, select
from sqlalchemy.exc import IntegrityError

from zeroth.econ.charge_costs import ChargeCostRevision
from zeroth.econ.plane.instrumentation.models import ChargeCostRevisionRecord, ExecutionEvent
from zeroth.econ.plane.scoped_session import ScopedSession


def _utc_naive(value: datetime) -> datetime:
    # Database timestamps use the existing UTC-without-offset storage convention.
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo is not None else value


def revision_assertions(row: ChargeCostRevisionRecord) -> dict:
    return {
        "tenant_id": row.tenant_id, "charge_id": row.charge_id,
        "asserted_at": _utc_naive(row.asserted_at),
        "token_cost_usd": row.token_cost_usd, "tool_cost_usd": row.tool_cost_usd,
        "compute_cost_usd": row.compute_cost_usd, "cost_measurement": row.cost_measurement,
        "reason": row.reason,
    }


def find_revision(db: ScopedSession, payload: ChargeCostRevision) -> ChargeCostRevisionRecord | None:
    return db.scalars(select(ChargeCostRevisionRecord).where(
        ChargeCostRevisionRecord.charge_id == payload.charge_id,
        ChargeCostRevisionRecord.asserted_at == _utc_naive(payload.asserted_at),
    )).one_or_none()


def ingest_revision(
    db: ScopedSession, payload: ChargeCostRevision,
) -> tuple[str, ChargeCostRevisionRecord]:
    if type(db) is not ScopedSession or db.scope is None:
        raise TypeError("charge cost revisions require a tenant-scoped session")
    owner = db.scalars(select(ExecutionEvent).where(
        ExecutionEvent.charge_id == payload.charge_id, ExecutionEvent.cost_role == "charge",
    )).one_or_none()
    if owner is None:
        raise ValueError("charge cost revision requires an existing owned charge")
    if _utc_naive(payload.asserted_at) <= _utc_naive(owner.timestamp):
        raise ValueError("cost revision must be asserted after its original execution")
    owner_identity = (owner.id, owner.execution_id, _utc_naive(owner.timestamp))
    row = ChargeCostRevisionRecord(
        tenant_id=db.scope.tenant_id, ingested_at=datetime.now(UTC).replace(tzinfo=None),
        **{**payload.model_dump(), "asserted_at": _utc_naive(payload.asserted_at)},
    )

    def duplicate(existing: ChargeCostRevisionRecord) -> tuple[str, ChargeCostRevisionRecord]:
        if revision_assertions(existing) != revision_assertions(row):
            raise ValueError("immutable charge cost revision conflicts with stored assertion")
        return "duplicate", existing

    existing = find_revision(db, payload)
    if existing is not None:
        return duplicate(existing)
    db.add(row)
    try:
        db.flush()
        # SQLite may have FK enforcement disabled. After the flush holds its
        # write transaction, verify the owner still exists before committing;
        # erasure cannot interleave its delete after this check. PostgreSQL's
        # foreign key supplies the corresponding lock/constraint protection.
        current_owner = db.execute(select(
            ExecutionEvent.id, ExecutionEvent.execution_id, ExecutionEvent.timestamp,
        ).where(ExecutionEvent.charge_id == payload.charge_id)).one_or_none()
        if current_owner is None or tuple(current_owner) != owner_identity:
            raise ValueError("charge owner changed while writing its cost revision")
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = find_revision(db, payload)
        if existing is None:
            raise
        return duplicate(existing)
    return "inserted", row


def revision_history(db: ScopedSession, charge_id: str, limit: int) -> list[ChargeCostRevision]:
    rows = db.scalars(select(ChargeCostRevisionRecord).where(
        ChargeCostRevisionRecord.charge_id == charge_id,
    ).order_by(ChargeCostRevisionRecord.asserted_at.desc()).limit(limit))
    return [ChargeCostRevision.model_validate({
        **{key: value for key, value in revision_assertions(row).items() if key != "tenant_id"},
        "asserted_at": row.asserted_at.replace(tzinfo=UTC),
    }) for row in rows]


@dataclass(frozen=True)
class CostAmounts:
    token_cost_usd: Decimal | None
    tool_cost_usd: Decimal | None
    compute_cost_usd: Decimal | None
    cost_measurement: str

    @property
    def total(self) -> Decimal:
        return sum((value or Decimal("0") for value in (
            self.token_cost_usd, self.tool_cost_usd, self.compute_cost_usd,
        )), Decimal("0"))


def resolve_costs(
    db: ScopedSession, events: list[ExecutionEvent],
) -> tuple[dict[int, CostAmounts], list[ChargeCostRevisionRecord]]:
    """Return effective costs without changing any source ORM row or attempt."""
    owned = [event for event in events if event.cost_role == "charge"]
    revisions: list[ChargeCostRevisionRecord] = []
    if owned:
        owner_exists = select(ExecutionEvent.id, ChargeCostRevisionRecord.id).where(
            ExecutionEvent.id.in_(bindparam(
                "charge_owner_ids", sorted({event.id for event in owned}),
                type_=Integer, expanding=True, literal_execute=True,
            )),
            ExecutionEvent.charge_id == ChargeCostRevisionRecord.charge_id,
        ).correlate(ChargeCostRevisionRecord)
        revisions = list(db.scalars(select(ChargeCostRevisionRecord).where(
            owner_exists.exists(),
            ChargeCostRevisionRecord.asserted_at <= datetime.now(UTC).replace(tzinfo=None),
        ).order_by(ChargeCostRevisionRecord.asserted_at.desc())))
    latest = {}
    for row in revisions:
        latest.setdefault(row.charge_id, row)
    costs = {}
    for event in events:
        assertion = latest.get(event.charge_id, event) if event.cost_role == "charge" else event
        costs[event.id] = CostAmounts(
            assertion.token_cost_usd, assertion.tool_cost_usd,
            assertion.compute_cost_usd, assertion.cost_measurement,
        )
    return costs, revisions
