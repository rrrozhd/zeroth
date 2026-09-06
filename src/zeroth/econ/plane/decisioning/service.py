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
    ChargeOwnership,
    EconomicDecision,
    EvidenceFingerprint,
    OutcomeSemantics,
    RunEvidence,
    SourceDelivery,
    VersionEvidence,
    compare_workflow_versions,
)
from zeroth.econ.measurement import MeasurementState
from zeroth.econ.source_inventory import (
    MAX_WINDOW_EXECUTIONS, SourceWindowInventory, execution_ids_digest,
)
from zeroth.econ.plane.cloud.entitlements import release_usage, reserve_usage
from zeroth.econ.plane.decisioning.schemas import (
    DecisionScheduleCreate,
    DecisionScheduleOut,
    VersionComparisonRequest,
)
from zeroth.econ.plane.decisioning.models import DecisionSchedule, EconomicDecisionRecord
from zeroth.econ.plane.debugger.models import OutcomeDefinition
from zeroth.econ.plane.debugger.service import _matches_definition
from zeroth.econ.plane.instrumentation.models import ExecutionEvent, OutcomeEvent
from zeroth.econ.plane.instrumentation.identity import outcomes_for_events, run_identity, workflow_filter
from zeroth.econ.plane.instrumentation.service import (
    _datetime_identity, _execution_identity_fields, _outcome_assertions,
)
from zeroth.econ.plane.scoped_session import ScopedSession


def _measurement(value: str) -> MeasurementState:
    try:
        return MeasurementState(value.lower())
    except ValueError:
        return MeasurementState.UNMEASURED


def _outcome_semantics(definition: OutcomeDefinition | None, outcome_type: str) -> OutcomeSemantics:
    if definition is None:
        return OutcomeSemantics(status="missing")
    rule = {
        "outcome_type": definition.outcome_type,
        "operator": definition.operator,
        "target": definition.target_json,
    }
    return OutcomeSemantics(
        status="defined" if definition.outcome_type == outcome_type else "type_mismatch",
        definition_digest=definition.definition_digest,
        rule_digest=hashlib.sha256(json.dumps(
            rule, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode()).hexdigest(),
    )


def _outcome_measurement(outcome: OutcomeEvent | None) -> MeasurementState:
    if outcome is None:
        return MeasurementState.UNMEASURED
    return (
        MeasurementState.MEASURED
        if outcome.provenance.upper() == "MEASURED"
        else MeasurementState.ESTIMATED
    )


def _run_cost(events: list[ExecutionEvent]) -> tuple[Decimal | None, MeasurementState]:
    events = [event for event in events if event.cost_role != "summary"]
    if not events:
        return None, MeasurementState.UNMEASURED
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


def _source_fingerprint(
    tenant_id: str, executions: list[ExecutionEvent], outcomes: list[OutcomeEvent],
) -> EvidenceFingerprint:
    """Bind a result to the assertions actually read, independent of receipt order."""

    def scalar(value: object) -> str:
        if isinstance(value, Decimal):
            # Equal decimal amounts must hash equally, without context rounding.
            if value == 0:
                return "0"
            amount = format(value, "f")
            return amount.rstrip("0").rstrip(".") if "." in amount else amount
        if isinstance(value, datetime):
            return value.isoformat(timespec="microseconds")
        raise TypeError(f"Unsupported evidence value: {type(value).__name__}")

    def digest(value: object) -> str:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False, default=scalar,
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    version = (
        "stored-assertions/2" if any(row.source_window_id is not None for row in executions)
        else "stored-assertions/1"
    )
    if any(row.cost_role in {"charge", "summary"} for row in executions):
        version = "stored-assertions/3"
    execution_assertions = [_execution_identity_fields(row) for row in executions]
    if version != "stored-assertions/3":
        for assertion in execution_assertions:
            assertion.pop("cost_role")
            assertion.pop("charge_id")
    if version == "stored-assertions/1":
        # Keep historical v1 bytes stable when the new field carries no assertion.
        for assertion in execution_assertions:
            assertion.pop("source_window_id")
    return EvidenceFingerprint(
        version=version,
        digest=digest({
            "version": version,
            "tenant_id": tenant_id,
            "executions": sorted(digest(assertion) for assertion in execution_assertions),
            "outcomes": sorted(digest(_outcome_assertions(row)) for row in outcomes),
        }),
        execution_records=len(executions), outcome_records=len(outcomes),
    )


def _version_from_store(
    db: ScopedSession,
    *,
    workflow: str,
    version: str,
    outcome_type: str,
    source_window: SourceWindowInventory | None = None,
) -> VersionEvidence:
    definition = db.scalars(select(OutcomeDefinition).where(
        OutcomeDefinition.workflow_id == workflow,
        OutcomeDefinition.workflow_version == version,
    )).one_or_none()
    semantics = _outcome_semantics(definition, outcome_type)
    statement = select(ExecutionEvent).where(workflow_filter(ExecutionEvent, workflow, version))
    if source_window is not None:
        statement = statement.where(
            ExecutionEvent.source_window_id == source_window.source_window_id,
        ).order_by(ExecutionEvent.id).limit(MAX_WINDOW_EXECUTIONS + 1)
    executions = list(db.scalars(statement))
    executions_by_run: dict[str, list[ExecutionEvent]] = defaultdict(list)
    for event in executions:
        executions_by_run[run_identity(event)[2]].append(event)
    delivery = None
    incomplete_runs: set[str] = set()
    if source_window is not None:
        expected = {run.run_id: run for run in source_window.runs}
        observed = set(executions_by_run)
        # Zero-record runs are declarations of technical closure, not missing dollars.
        missing = sum(run.execution_count > 0 and run.run_id not in observed
                      for run in source_window.runs)
        unexpected = len(observed - expected.keys())
        mismatched = {
            run_id for run_id, run in expected.items()
            if len(executions_by_run.get(run_id, [])) != run.execution_count
            or execution_ids_digest([
                event.execution_id for event in executions_by_run.get(run_id, [])
            ]) != run.execution_ids_digest
        }
        opened = source_window.opened_at.replace(tzinfo=None)
        closed = source_window.closed_at.replace(tzinfo=None)
        outside = [event for event in executions
                   if not opened <= _datetime_identity(event.timestamp) <= closed]
        truncated = len(executions) > MAX_WINDOW_EXECUTIONS
        incomplete_runs = mismatched | (observed - expected.keys()) | {
            run_identity(event)[2] for event in outside
        }
        if truncated:
            incomplete_runs |= observed
        delivery = SourceDelivery(
            source_window_id=source_window.source_window_id,
            inventory_digest=source_window.digest(),
            status="mismatch" if missing or unexpected or mismatched or outside or truncated else "matched",
            expected_runs=len(expected), observed_runs=len(observed),
            expected_executions=sum(run.execution_count for run in source_window.runs),
            observed_executions=len(executions), missing_runs=missing,
            unexpected_runs=unexpected, mismatched_runs=len(mismatched),
            out_of_window_executions=len(outside), scan_truncated=truncated,
        )
        for run_id in expected:
            executions_by_run.setdefault(run_id, [])
    outcome_by_run = {}
    selected_outcomes = outcomes_for_events(db, executions, outcome_type=outcome_type)
    for key, outcome in selected_outcomes:
        outcome_by_run.setdefault(key[2], outcome)

    runs: list[RunEvidence] = []
    for run_id, run_events in sorted(executions_by_run.items()):
        cost, cost_measurement = _run_cost([] if run_id in incomplete_runs else run_events)
        outcome = outcome_by_run.get(run_id)
        runs.append(
            RunEvidence(
                run_id=run_id,
                cost_usd=cost,
                cost_measurement=cost_measurement,
                accepted=(
                    _matches_definition(outcome, definition)
                    if outcome is not None and semantics.status == "defined" else None
                ),
                outcome_measurement=_outcome_measurement(outcome),
            )
        )
    owned = sum(event.cost_role == "charge" for event in executions)
    summaries = sum(event.cost_role == "summary" for event in executions)
    unattributed = len(executions) - owned - summaries
    return VersionEvidence(
        workflow=workflow, version=version, runs=runs,
        outcome_semantics=semantics,
        source_delivery=delivery,
        charge_ownership=ChargeOwnership(
            status="declared" if owned and not unattributed else "unverified",
            owned_charge_records=owned, summary_records=summaries,
            unattributed_records=unattributed,
        ),
        source_fingerprint=_source_fingerprint(
            db.scope.tenant_id, executions, [outcome for _, outcome in selected_outcomes],
        ),
    )


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
        source_window=request.source_windows.get("baseline"),
    )
    candidate = _version_from_store(
        db,
        workflow=request.workflow,
        version=request.candidate_version,
        outcome_type=request.outcome_type,
        source_window=request.source_windows.get("candidate"),
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
