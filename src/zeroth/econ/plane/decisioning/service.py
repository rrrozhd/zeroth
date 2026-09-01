"""Normalize stored execution/outcome evidence into economic decisions."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import json

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from zeroth.econ.decisioning import (
    EconomicDecision,
    RunEvidence,
    VersionEvidence,
    compare_workflow_versions,
)
from zeroth.econ.measurement import MeasurementState
from zeroth.econ.plane.cloud.entitlements import release_usage, reserve_usage
from zeroth.econ.plane.decisioning.schemas import (
    DecisionScheduleCreate,
    DecisionScheduleOut,
    VersionComparisonRequest,
)
from zeroth.econ.plane.decisioning.models import DecisionSchedule, EconomicDecisionRecord
from zeroth.econ.plane.instrumentation.models import ExecutionEvent, OutcomeEvent
from zeroth.econ.plane.scoped_session import ScopedSession


def _measurement(value: str) -> MeasurementState:
    try:
        return MeasurementState(value.lower())
    except ValueError:
        return MeasurementState.UNMEASURED


def _accepted(outcome: OutcomeEvent | None) -> bool | None:
    if outcome is None:
        return None
    value = (outcome.outcome_payload_json or {}).get("accepted")
    if type(value) is bool:
        return value
    raw = outcome.outcome_value.strip().lower()
    if raw in {"true", "1", "yes", "accepted", "success"}:
        return True
    if raw in {"false", "0", "no", "rejected", "failure"}:
        return False
    return None


def _outcome_measurement(outcome: OutcomeEvent | None) -> MeasurementState:
    if outcome is None:
        return MeasurementState.UNMEASURED
    return (
        MeasurementState.MEASURED
        if outcome.provenance.upper() == "MEASURED"
        else MeasurementState.ESTIMATED
    )


def _run_cost(events: list[ExecutionEvent]) -> tuple[Decimal | None, MeasurementState]:
    states = {_measurement(event.cost_measurement) for event in events}
    if MeasurementState.UNMEASURED in states:
        return None, MeasurementState.UNMEASURED
    total = sum(
        (
            (event.token_cost_usd or Decimal("0"))
            + (event.tool_cost_usd or Decimal("0"))
            + (event.compute_cost_usd or Decimal("0"))
            for event in events
        ),
        Decimal("0"),
    )
    state = (
        MeasurementState.ESTIMATED
        if MeasurementState.ESTIMATED in states
        else MeasurementState.MEASURED
    )
    return total, state


def _version_from_store(
    db: ScopedSession,
    *,
    workflow: str,
    version: str,
    outcome_type: str,
) -> VersionEvidence:
    executions = list(
        db.scalars(
            select(ExecutionEvent).where(
                ExecutionEvent.capability_id == workflow,
                ExecutionEvent.implementation_id == version,
            )
        )
    )
    outcomes = list(
        db.scalars(
            select(OutcomeEvent)
            .where(
                OutcomeEvent.capability_id == workflow,
                OutcomeEvent.implementation_id == version,
                OutcomeEvent.outcome_type == outcome_type,
            )
            .order_by(OutcomeEvent.occurred_at)
        )
    )

    executions_by_run: dict[str, list[ExecutionEvent]] = defaultdict(list)
    for event in executions:
        executions_by_run[event.join_key or event.execution_id].append(event)
    outcome_by_run = {outcome.join_key or outcome.execution_id: outcome for outcome in outcomes}

    runs: list[RunEvidence] = []
    for run_id, run_events in sorted(executions_by_run.items()):
        cost, cost_measurement = _run_cost(run_events)
        outcome = outcome_by_run.get(run_id)
        runs.append(
            RunEvidence(
                run_id=run_id,
                cost_usd=cost,
                cost_measurement=cost_measurement,
                accepted=_accepted(outcome),
                outcome_measurement=_outcome_measurement(outcome),
            )
        )
    return VersionEvidence(workflow=workflow, version=version, runs=runs)


def compare_versions_from_store(
    db: ScopedSession,
    request: VersionComparisonRequest,
) -> EconomicDecision:
    """Read two tenant-scoped versions and apply the shared decision policy."""

    if type(db) is not ScopedSession:
        raise TypeError("economic decisions require a ScopedSession")
    baseline = _version_from_store(
        db,
        workflow=request.workflow,
        version=request.baseline_version,
        outcome_type=request.outcome_type,
    )
    candidate = _version_from_store(
        db,
        workflow=request.workflow,
        version=request.candidate_version,
        outcome_type=request.outcome_type,
    )
    return compare_workflow_versions(baseline, candidate, policy=request.policy)


def _stored_decision(record: EconomicDecisionRecord) -> EconomicDecision:
    evaluated_at = record.evaluated_at
    if evaluated_at.tzinfo is None:
        evaluated_at = evaluated_at.replace(tzinfo=UTC)
    return EconomicDecision.model_validate(record.report_json).model_copy(
        update={
            "decision_id": record.decision_id,
            "evaluated_at": evaluated_at,
        }
    )


def retain_decision(
    db: ScopedSession,
    request: VersionComparisonRequest,
    decision: EconomicDecision,
    *,
    evaluated_by: str,
) -> EconomicDecision:
    """Persist one immutable decision, deduplicated by request and evidence."""

    if type(db) is not ScopedSession or db.scope is None:
        raise TypeError("retained economic decisions require a tenant-scoped session")
    report_json = decision.model_dump(
        mode="json", exclude={"decision_id", "evaluated_at"}
    )
    digest_payload = {
        "request": request.model_dump(mode="json"),
        "report": report_json,
    }
    evidence_digest = hashlib.sha256(
        json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    decision_id = f"dec_{hashlib.sha256(f'{db.scope.tenant_id}:{evidence_digest}'.encode()).hexdigest()[:24]}"
    existing = db.get(EconomicDecisionRecord, decision_id)
    if existing is not None:
        return _stored_decision(existing)

    now = datetime.now(UTC)
    record = EconomicDecisionRecord(
        decision_id=decision_id,
        tenant_id=db.scope.tenant_id,
        evidence_digest=evidence_digest,
        workflow=request.workflow,
        baseline_version=request.baseline_version,
        candidate_version=request.candidate_version,
        outcome_type=request.outcome_type,
        verdict=decision.verdict,
        recommended_action=decision.recommended_action,
        report_json=report_json,
        evaluated_at=now,
        evaluated_by=evaluated_by,
    )
    db.add(record)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        concurrent = db.get(EconomicDecisionRecord, decision_id)
        if concurrent is None:
            raise
        return _stored_decision(concurrent)
    return decision.model_copy(update={"decision_id": decision_id, "evaluated_at": now})


def list_retained_decisions(
    db: ScopedSession,
    *,
    workflow: str | None = None,
    limit: int = 50,
) -> list[EconomicDecision]:
    if type(db) is not ScopedSession:
        raise TypeError("decision history requires a ScopedSession")
    statement = select(EconomicDecisionRecord)
    if workflow is not None:
        statement = statement.where(EconomicDecisionRecord.workflow == workflow)
    records = list(
        db.scalars(
            statement.order_by(EconomicDecisionRecord.evaluated_at.desc()).limit(
                max(1, min(limit, 200))
            )
        )
    )
    return [_stored_decision(record) for record in records]


def _schedule_out(schedule: DecisionSchedule) -> DecisionScheduleOut:
    def utc(value: datetime | None) -> datetime | None:
        if value is None or value.tzinfo is not None:
            return value
        return value.replace(tzinfo=UTC)

    return DecisionScheduleOut.model_validate(
        {
            "schedule_id": schedule.schedule_id,
            "workflow": schedule.workflow,
            "baseline_version": schedule.baseline_version,
            "candidate_version": schedule.candidate_version,
            "outcome_type": schedule.outcome_type,
            "policy": schedule.policy_json,
            "interval_minutes": schedule.interval_minutes,
            "active": schedule.active,
            "next_run_at": utc(schedule.next_run_at),
            "last_run_at": utc(schedule.last_run_at),
            "last_decision_id": schedule.last_decision_id,
            "last_error": schedule.last_error,
            "created_at": utc(schedule.created_at),
        }
    )


def create_decision_schedule(
    db: ScopedSession,
    payload: DecisionScheduleCreate,
    *,
    created_by: str,
) -> DecisionScheduleOut:
    if type(db) is not ScopedSession or db.scope is None:
        raise TypeError("decision schedules require a tenant-scoped session")
    import uuid

    now = datetime.now(UTC)
    schedule = DecisionSchedule(
        schedule_id=f"dsch_{uuid.uuid4().hex[:24]}",
        tenant_id=db.scope.tenant_id,
        workflow=payload.workflow,
        baseline_version=payload.baseline_version,
        candidate_version=payload.candidate_version,
        outcome_type=payload.outcome_type,
        policy_json=payload.policy.model_dump(mode="json"),
        interval_minutes=payload.interval_minutes,
        active=True,
        next_run_at=now,
        last_run_at=None,
        last_decision_id=None,
        last_error=None,
        created_at=now,
        updated_at=now,
        created_by=created_by,
    )
    db.add(schedule)
    db.commit()
    return _schedule_out(schedule)


def list_decision_schedules(db: ScopedSession) -> list[DecisionScheduleOut]:
    if type(db) is not ScopedSession:
        raise TypeError("decision schedules require a ScopedSession")
    rows = list(
        db.scalars(select(DecisionSchedule).order_by(DecisionSchedule.created_at.desc()))
    )
    return [_schedule_out(row) for row in rows]


def run_due_decision_schedules(
    db: ScopedSession,
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> list[EconomicDecision]:
    """Claim and evaluate due schedules for the bound tenant."""

    if type(db) is not ScopedSession:
        raise TypeError("scheduled decisions require a ScopedSession")
    current = now or datetime.now(UTC)
    due = list(
        db.scalars(
            select(DecisionSchedule)
            .where(
                DecisionSchedule.active.is_(True),
                DecisionSchedule.next_run_at <= current,
            )
            .order_by(DecisionSchedule.next_run_at)
            .limit(max(1, min(limit, 500)))
        )
    )
    completed: list[EconomicDecision] = []
    for due_schedule in due:
        next_run = current + timedelta(minutes=due_schedule.interval_minutes)
        claimed = db.execute(
            update(DecisionSchedule)
            .where(
                DecisionSchedule.schedule_id == due_schedule.schedule_id,
                DecisionSchedule.active.is_(True),
                DecisionSchedule.next_run_at <= current,
            )
            .values(next_run_at=next_run, updated_at=current)
            .returning(DecisionSchedule.schedule_id)
            .execution_options(synchronize_session=False)
        ).scalar_one_or_none()
        db.commit()
        if claimed is None:
            continue
        request = VersionComparisonRequest(
            workflow=due_schedule.workflow,
            baseline_version=due_schedule.baseline_version,
            candidate_version=due_schedule.candidate_version,
            outcome_type=due_schedule.outcome_type,
            policy=due_schedule.policy_json,
        )
        schedule = db.get(DecisionSchedule, due_schedule.schedule_id)
        assert schedule is not None
        reserved = False
        try:
            reserved = reserve_usage(db, "decision_scans")
            decision = compare_versions_from_store(db, request)
            decision = retain_decision(
                db,
                request,
                decision,
                evaluated_by=f"schedule:{schedule.schedule_id}",
            )
            schedule.last_run_at = current
            schedule.last_decision_id = decision.decision_id
            schedule.last_error = None
            schedule.updated_at = current
            db.commit()
            completed.append(decision)
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            if reserved:
                release_usage(db, "decision_scans")
            schedule = db.get(DecisionSchedule, due_schedule.schedule_id)
            if schedule is not None:
                schedule.last_run_at = current
                schedule.last_error = str(exc)[:512]
                schedule.updated_at = current
                db.commit()
    return completed
