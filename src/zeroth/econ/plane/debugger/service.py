"""Bounded, tenant-scoped economic-debugger aggregations."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
import json
from typing import Literal

from sqlalchemy import select

from zeroth.econ.measurement import MeasurementState
from zeroth.econ.plane.debugger.models import OutcomeDefinition
from zeroth.econ.plane.debugger.schemas import (
    BreakagePoint,
    CohortPoint,
    DiagnosticAction,
    EconomicDiagnosticReport,
    OutcomeDefinitionCreate,
    TimelinePoint,
)
from zeroth.econ.plane.instrumentation.models import ExecutionEvent, OutcomeEvent
from zeroth.econ.plane.scoped_session import ScopedSession

MAX_DEBUGGER_EVENTS = 50_000
_UNKNOWN = "(unknown)"
_LIMITATIONS = [
    "Failed-run exposure identifies where money accumulated, not which step caused the failure.",
    "Estimated cost is kept separate and is not provider-billed ground truth.",
    "Outcome success follows an immutable workflow-version definition; undefined versions remain unresolved.",
    "This report observes production history; it does not prove savings from an untested change.",
]


def _require_exact_scoped_session(db: object) -> ScopedSession:
    if type(db) is not ScopedSession:
        raise TypeError("economic debugger persistence requires an exact ScopedSession")
    return db


def _outcome_value(outcome: OutcomeEvent) -> object:
    payload = outcome.outcome_payload_json or {}
    return payload.get("value", outcome.outcome_value)


def _matches_definition(outcome: OutcomeEvent, definition: OutcomeDefinition) -> bool | None:
    value = _outcome_value(outcome)
    target = definition.target_json
    operator = definition.operator
    if operator in {"greater_than_or_equal", "less_than_or_equal"}:
        if isinstance(value, bool) or isinstance(target, bool):
            return None
        try:
            observed = Decimal(str(value))
            boundary = Decimal(str(target))
        except Exception:  # noqa: BLE001
            return None
        if operator == "greater_than_or_equal":
            return observed >= boundary
        return observed <= boundary
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        equal = (
            isinstance(target, (int, float))
            and not isinstance(target, bool)
            and Decimal(str(value)) == Decimal(str(target))
        )
    else:
        equal = type(value) is type(target) and value == target
    return equal if operator == "equals" else not equal


def _definition_digest(payload: OutcomeDefinitionCreate) -> str:
    material = json.dumps(
        payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    return f"sha256:{sha256(material.encode()).hexdigest()}"


def create_outcome_definition(
    db: ScopedSession, payload: OutcomeDefinitionCreate
) -> tuple[bool, OutcomeDefinition]:
    db = _require_exact_scoped_session(db)
    digest = _definition_digest(payload)
    existing = db.execute(
        select(OutcomeDefinition).where(
            OutcomeDefinition.workflow_id == payload.workflow_id,
            OutcomeDefinition.workflow_version == payload.workflow_version,
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.definition_digest != digest:
            raise ValueError("Outcome definition is immutable for this workflow version")
        return False, existing
    row = OutcomeDefinition(
        workflow_id=payload.workflow_id,
        workflow_version=payload.workflow_version,
        outcome_type=payload.outcome_type,
        operator=payload.operator,
        target_json=payload.target,
        definition_digest=digest,
        created_at=datetime.now(UTC),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return True, row


def list_outcome_definitions(
    db: ScopedSession, *, workflow_id: str | None = None
) -> list[OutcomeDefinition]:
    db = _require_exact_scoped_session(db)
    statement = select(OutcomeDefinition)
    if workflow_id is not None:
        statement = statement.where(OutcomeDefinition.workflow_id == workflow_id)
    return list(
        db.execute(
            statement.order_by(
                OutcomeDefinition.workflow_id, OutcomeDefinition.workflow_version
            )
        ).scalars()
    )


def resolve_outcomes_for_events(
    db: ScopedSession, events: list[ExecutionEvent]
) -> dict[str, bool]:
    """Resolve run outcomes consistently for debugger and financial reports."""

    db = _require_exact_scoped_session(db)
    identities_by_run: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for event in events:
        if event.run_id and event.workflow_id and event.workflow_version:
            identities_by_run[event.run_id].add(
                (event.workflow_id, event.workflow_version)
            )
    run_ids = set(identities_by_run)
    if not run_ids:
        return {}
    workflows = {
        workflow for identities in identities_by_run.values() for workflow, _ in identities
    }
    versions = {
        version for identities in identities_by_run.values() for _, version in identities
    }
    definitions = {
        (row.workflow_id, row.workflow_version): row
        for row in db.execute(
            select(OutcomeDefinition).where(
                OutcomeDefinition.workflow_id.in_(workflows),
                OutcomeDefinition.workflow_version.in_(versions),
            )
        ).scalars()
    }
    outcomes = list(
        db.execute(
            select(OutcomeEvent)
            .where(OutcomeEvent.join_key.in_(run_ids))
            .order_by(OutcomeEvent.occurred_at.desc(), OutcomeEvent.id.desc())
            .limit(MAX_DEBUGGER_EVENTS)
        ).scalars()
    )
    status: dict[str, bool] = {}
    for outcome in outcomes:
        if outcome.join_key in status:
            continue
        identities = identities_by_run.get(outcome.join_key, set())
        if len(identities) != 1:
            continue
        definition = definitions.get(next(iter(identities)))
        if definition is None or outcome.outcome_type != definition.outcome_type:
            continue
        accepted = _matches_definition(outcome, definition)
        if accepted is not None:
            status[outcome.join_key] = accepted
    return status


def _load_evidence(
    db: ScopedSession,
    *,
    workflow_id: str,
    start: datetime | None,
    end: datetime | None,
) -> tuple[list[ExecutionEvent], dict[str, bool]]:
    db = _require_exact_scoped_session(db)
    statement = select(ExecutionEvent).where(ExecutionEvent.workflow_id == workflow_id)
    if start is not None:
        statement = statement.where(ExecutionEvent.timestamp >= start)
    if end is not None:
        statement = statement.where(ExecutionEvent.timestamp < end)
    events = list(
        db.execute(
            statement.order_by(ExecutionEvent.timestamp.desc(), ExecutionEvent.id.desc()).limit(
                MAX_DEBUGGER_EVENTS
            )
        ).scalars()
    )
    events.reverse()
    return events, resolve_outcomes_for_events(db, events)


def _cost(event: ExecutionEvent) -> tuple[Decimal, Decimal, bool]:
    total = sum(
        (
            value or Decimal("0")
            for value in (
                event.token_cost_usd,
                event.tool_cost_usd,
                event.compute_cost_usd,
            )
        ),
        Decimal("0"),
    )
    if event.cost_measurement == MeasurementState.MEASURED.value:
        return total, Decimal("0"), False
    if event.cost_measurement == MeasurementState.ESTIMATED.value:
        return Decimal("0"), total, False
    return Decimal("0"), Decimal("0"), True


def _round(value: Decimal) -> float:
    return float(round(value, 8))


def _ratio(value: Decimal, denominator: int) -> float | None:
    return _round(value / denominator) if denominator else None


def _incomplete(event: ExecutionEvent, missing_cost: bool) -> bool:
    return bool(
        missing_cost
        or not event.workflow_id
        or not event.workflow_version
        or not event.run_id
        or not event.step_id
    )


def timeline(
    db: ScopedSession,
    *,
    workflow_id: str,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[TimelinePoint]:
    events, outcomes = _load_evidence(db, workflow_id=workflow_id, start=start, end=end)
    groups: dict[tuple[datetime, str], list[ExecutionEvent]] = defaultdict(list)
    for event in events:
        period = event.timestamp.replace(hour=0, minute=0, second=0, microsecond=0)
        groups[(period, event.workflow_version or _UNKNOWN)].append(event)

    points: list[TimelinePoint] = []
    for (period, version), rows in sorted(groups.items()):
        run_ids = {row.run_id for row in rows if row.run_id}
        successful = {run_id for run_id in run_ids if outcomes.get(run_id) is True}
        failed = {run_id for run_id in run_ids if outcomes.get(run_id) is False}
        measured = estimated = measured_failure = estimated_failure = Decimal("0")
        incomplete = 0
        for row in rows:
            row_measured, row_estimated, missing = _cost(row)
            measured += row_measured
            estimated += row_estimated
            if row.run_id in failed:
                measured_failure += row_measured
                estimated_failure += row_estimated
            incomplete += int(_incomplete(row, missing))
        points.append(
            TimelinePoint(
                period_start=period,
                workflow_id=workflow_id,
                workflow_version=version,
                runs=len(run_ids),
                successful_runs=len(successful),
                failed_runs=len(failed),
                measured_cost_usd=_round(measured),
                estimated_cost_usd=_round(estimated),
                measured_failure_exposure_usd=_round(measured_failure),
                estimated_failure_exposure_usd=_round(estimated_failure),
                measured_cost_per_successful_outcome_usd=_ratio(measured, len(successful)),
                estimated_cost_per_successful_outcome_usd=_ratio(estimated, len(successful)),
                incomplete_events=incomplete,
            )
        )
    return points


def cohorts(
    db: ScopedSession,
    *,
    workflow_id: str,
    group_by: Literal["subject_id", "dimension"],
    dimension: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[CohortPoint]:
    if group_by == "dimension" and not dimension:
        raise ValueError("dimension is required when group_by=dimension")
    events, outcomes = _load_evidence(db, workflow_id=workflow_id, start=start, end=end)
    groups: dict[str, list[ExecutionEvent]] = defaultdict(list)
    for event in events:
        cohort = event.subject_id if group_by == "subject_id" else event.dimensions.get(dimension)
        groups[str(cohort) if cohort not in (None, "") else _UNKNOWN].append(event)

    points: list[CohortPoint] = []
    for cohort, rows in sorted(groups.items()):
        run_ids = {row.run_id for row in rows if row.run_id}
        successful = {run_id for run_id in run_ids if outcomes.get(run_id) is True}
        failed = {run_id for run_id in run_ids if outcomes.get(run_id) is False}
        measured = estimated = Decimal("0")
        incomplete = 0
        for row in rows:
            row_measured, row_estimated, missing = _cost(row)
            measured += row_measured
            estimated += row_estimated
            incomplete += int(missing or cohort == _UNKNOWN)
        points.append(
            CohortPoint(
                cohort=cohort,
                runs=len(run_ids),
                successful_runs=len(successful),
                failed_runs=len(failed),
                measured_cost_usd=_round(measured),
                estimated_cost_usd=_round(estimated),
                measured_cost_per_successful_outcome_usd=_ratio(measured, len(successful)),
                estimated_cost_per_successful_outcome_usd=_ratio(estimated, len(successful)),
                incomplete_events=incomplete,
            )
        )
    return points


def breakage(
    db: ScopedSession,
    *,
    workflow_id: str,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[BreakagePoint]:
    events, outcomes = _load_evidence(db, workflow_id=workflow_id, start=start, end=end)
    failed_runs = {run_id for run_id, accepted in outcomes.items() if accepted is False}
    groups: dict[tuple[str, str], list[ExecutionEvent]] = defaultdict(list)
    for event in events:
        if event.run_id in failed_runs:
            groups[(event.workflow_version or _UNKNOWN, event.step_id or _UNKNOWN)].append(event)

    points: list[BreakagePoint] = []
    for (version, step_id), rows in groups.items():
        measured = estimated = repeated_measured = repeated_estimated = Decimal("0")
        for row in rows:
            row_measured, row_estimated, _missing = _cost(row)
            measured += row_measured
            estimated += row_estimated
            if row.attempt > 1:
                repeated_measured += row_measured
                repeated_estimated += row_estimated
        points.append(
            BreakagePoint(
                workflow_id=workflow_id,
                workflow_version=version,
                step_id=step_id,
                failed_runs=len({row.run_id for row in rows if row.run_id}),
                measured_failure_exposure_usd=_round(measured),
                estimated_failure_exposure_usd=_round(estimated),
                measured_repeated_attempt_cost_usd=_round(repeated_measured),
                estimated_repeated_attempt_cost_usd=_round(repeated_estimated),
            )
        )
    return sorted(
        points,
        key=lambda point: (
            point.measured_failure_exposure_usd + point.estimated_failure_exposure_usd,
            point.workflow_version,
            point.step_id,
        ),
        reverse=True,
    )


def _diagnostic_action(
    *,
    incomplete_events: int,
    undefined_outcome_versions: list[str],
    unresolved_runs: int,
    top_failure: BreakagePoint | None,
) -> DiagnosticAction:
    if incomplete_events:
        return DiagnosticAction(
            code="repair_evidence",
            rationale=f"{incomplete_events} event(s) lack complete identity or cost evidence.",
            supported_claim=(
                "The economic picture is incomplete; repair evidence before deciding what "
                "to change."
            ),
        )
    if undefined_outcome_versions:
        return DiagnosticAction(
            code="define_outcome_success",
            rationale=(
                f"{len(undefined_outcome_versions)} workflow version(s) have no immutable "
                "outcome definition."
            ),
            supported_claim=(
                "Cost is observed, but success and failure remain undefined for those versions."
            ),
        )
    if unresolved_runs:
        return DiagnosticAction(
            code="instrument_outcomes",
            rationale=f"{unresolved_runs} run(s) have no resolved outcome.",
            supported_claim=(
                "Cost is observed, but cost per successful outcome is not decision-ready."
            ),
        )
    if top_failure is not None:
        repeated = Decimal(str(top_failure.measured_repeated_attempt_cost_usd))
        repeated_estimated = Decimal(str(top_failure.estimated_repeated_attempt_cost_usd))
        if repeated > 0 or repeated_estimated > 0:
            channel = "measured" if repeated > 0 else "estimated"
            amount = repeated if repeated > 0 else repeated_estimated
            return DiagnosticAction(
                code="investigate_retry_policy",
                rationale=(
                    f"${amount:.8f} {channel} cost came from explicit repeated attempts "
                    "in failed runs."
                ),
                supported_claim=(
                    "Repeated-attempt cost is observed; whether changing retries preserves "
                    "outcomes is unproven."
                ),
            )
        return DiagnosticAction(
            code="inspect_failed_runs",
            rationale=(
                f"Step {top_failure.step_id!r} contains the largest observed failed-run "
                "cost exposure."
            ),
            supported_claim=(
                "The step is a useful investigation boundary, not a proven cause of failure."
            ),
        )
    return DiagnosticAction(
        code="retain_current_configuration",
        rationale="No failed-run cost exposure is present in the selected evidence window.",
        supported_claim="No economic change is supported by this window alone.",
    )


def diagnostic_report(
    db: ScopedSession,
    *,
    workflow_id: str,
    start: datetime | None = None,
    end: datetime | None = None,
    cohort_dimension: str | None = None,
) -> EconomicDiagnosticReport | None:
    events, outcomes = _load_evidence(db, workflow_id=workflow_id, start=start, end=end)
    if not events:
        return None

    run_ids = {event.run_id for event in events if event.run_id}
    successful = {run_id for run_id in run_ids if outcomes.get(run_id) is True}
    failed = {run_id for run_id in run_ids if outcomes.get(run_id) is False}
    unresolved = run_ids - successful - failed
    workflow_versions = sorted(
        {event.workflow_version for event in events if event.workflow_version}
    )
    defined_versions = {
        row.workflow_version
        for row in db.execute(
            select(OutcomeDefinition).where(
                OutcomeDefinition.workflow_id == workflow_id,
                OutcomeDefinition.workflow_version.in_(workflow_versions),
            )
        ).scalars()
    }
    undefined_outcome_versions = [
        version for version in workflow_versions if version not in defined_versions
    ]
    measured = estimated = measured_failure = estimated_failure = Decimal("0")
    measured_events = estimated_events = unmeasured_events = incomplete_events = 0
    for event in events:
        event_measured, event_estimated, missing_cost = _cost(event)
        measured += event_measured
        estimated += event_estimated
        if event.run_id in failed:
            measured_failure += event_measured
            estimated_failure += event_estimated
        measured_events += int(event.cost_measurement == MeasurementState.MEASURED.value)
        estimated_events += int(event.cost_measurement == MeasurementState.ESTIMATED.value)
        unmeasured_events += int(missing_cost)
        incomplete_events += int(_incomplete(event, missing_cost))

    failure_points = breakage(db, workflow_id=workflow_id, start=start, end=end)
    top_failure_exposure = failure_points[0] if failure_points else None
    highest_failure_rate_cohort = None
    if cohort_dimension:
        candidates = [
            point
            for point in cohorts(
                db,
                workflow_id=workflow_id,
                group_by="dimension",
                dimension=cohort_dimension,
                start=start,
                end=end,
            )
            if point.cohort != _UNKNOWN and point.successful_runs + point.failed_runs > 0
        ]
        if candidates:
            highest_failure_rate_cohort = max(
                candidates,
                key=lambda point: (
                    point.failed_runs / (point.successful_runs + point.failed_runs),
                    point.measured_cost_usd + point.estimated_cost_usd,
                    point.cohort,
                ),
            )

    if incomplete_events:
        data_quality = "incomplete"
    elif measured_events and estimated_events:
        data_quality = "mixed_cost_evidence"
    elif estimated_events:
        data_quality = "estimated_only"
    else:
        data_quality = "measured_only"
    if incomplete_events or unresolved:
        decision_state = "insufficient_evidence"
    elif measured_failure > 0 or estimated_failure > 0:
        decision_state = "economic_risk_observed"
    else:
        decision_state = "stable_observation"

    return EconomicDiagnosticReport(
        workflow_id=workflow_id,
        window_start=start,
        window_end=end,
        cohort_dimension=cohort_dimension,
        decision_state=decision_state,
        data_quality=data_quality,
        event_count=len(events),
        runs=len(run_ids),
        successful_runs=len(successful),
        failed_runs=len(failed),
        unresolved_runs=len(unresolved),
        undefined_outcome_versions=undefined_outcome_versions,
        outcome_coverage=_round(
            Decimal(len(successful) + len(failed)) / Decimal(len(run_ids))
            if run_ids
            else Decimal("0")
        ),
        measured_events=measured_events,
        estimated_events=estimated_events,
        unmeasured_events=unmeasured_events,
        incomplete_events=incomplete_events,
        measured_cost_usd=_round(measured),
        estimated_cost_usd=_round(estimated),
        measured_failure_exposure_usd=_round(measured_failure),
        estimated_failure_exposure_usd=_round(estimated_failure),
        measured_cost_per_successful_outcome_usd=_ratio(measured, len(successful)),
        estimated_cost_per_successful_outcome_usd=_ratio(estimated, len(successful)),
        top_failure_exposure=top_failure_exposure,
        highest_failure_rate_cohort=highest_failure_rate_cohort,
        recommended_action=_diagnostic_action(
            incomplete_events=incomplete_events,
            undefined_outcome_versions=undefined_outcome_versions,
            unresolved_runs=len(unresolved),
            top_failure=top_failure_exposure,
        ),
        limitations=list(_LIMITATIONS),
    )
