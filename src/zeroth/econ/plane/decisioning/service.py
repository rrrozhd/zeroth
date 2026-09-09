"""Normalize stored execution/outcome evidence into economic decisions."""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from math import ceil
from statistics import fmean

from pydantic import ValidationError
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
from zeroth.econ.plane.cloud.entitlements import release_usage, reserve_usage
from zeroth.econ.plane.debugger.models import OutcomeDefinition
from zeroth.econ.plane.debugger.service import _matches_definition
from zeroth.econ.plane.decisioning.evidence import harvest_migration_evidence
from zeroth.econ.plane.decisioning.models import (
    DecisionSchedule,
    EconomicDecisionRecord,
    ForecastCalibrationRecord,
    ProbabilisticDecisionSchedule,
    ProbabilisticMigrationDecisionRecord,
    RandomizedRolloutAssignmentRecord,
    RandomizedRolloutRecord,
    RandomizedRolloutVerificationRecord,
)
from zeroth.econ.plane.decisioning.qualification import (
    QualificationApplicabilityV1,
    QualificationResolution,
    resolve_qualification,
)
from zeroth.econ.plane.decisioning.schemas import (
    DecisionScheduleCreate,
    DecisionScheduleOut,
    MigrationEvidenceRefreshRequest,
    MigrationEvidenceSource,
    ProbabilisticDecisionScheduleCreate,
    ProbabilisticDecisionScheduleOut,
    ProbabilisticMigrationRequest,
    RandomizedRolloutAssignmentOut,
    RandomizedRolloutCreate,
    RandomizedRolloutOut,
    VersionComparisonRequest,
)
from zeroth.econ.plane.instrumentation.charge_costs import (
    CostAmounts,
    resolve_costs,
    revision_assertions,
)
from zeroth.econ.plane.instrumentation.identity import (
    outcomes_for_events,
    run_identity,
    workflow_filter,
)
from zeroth.econ.plane.instrumentation.models import ExecutionEvent, OutcomeEvent
from zeroth.econ.plane.instrumentation.service import (
    _datetime_identity,
    _execution_identity_fields,
    _outcome_assertions,
)
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.econ.probabilistic import (
    FORECAST_ALGORITHM_VERSION,
    ForecastCalibrationObservation,
    ProbabilisticMigrationDecision,
    assess_forecast_readiness,
    recommend_model_migration,
)
from zeroth.econ.rollout_verification import (
    RandomizedRolloutPlan,
    RolloutAssignment,
    RolloutObservation,
    RolloutVerification,
    assign_rollout_arm,
    verify_randomized_rollout,
)
from zeroth.econ.source_inventory import (
    MAX_WINDOW_EXECUTIONS,
    SourceWindowInventory,
    execution_ids_digest,
)

_MODEL_MIGRATION_CALIBRATION_METRICS = {
    "monthly_cost_usd",
    "success_rate",
    "p95_latency_ms",
    "critical_error_rate",
}
_ROLLOUT_VERIFICATION_ALGORITHM_VERSION = "homogeneous-assignment-v1"


class RandomizedRolloutInactiveError(ValueError):
    """A stopped rollout cannot accept a new subject assignment."""
_SCHEDULE_FAILURE_CODE = "schedule_execution_failed"
logger = logging.getLogger(__name__)


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


def _run_cost(events: list[ExecutionEvent], costs: dict[int, CostAmounts]) -> tuple[Decimal | None, MeasurementState]:
    events = [event for event in events if event.cost_role != "summary"]
    if not events:
        return None, MeasurementState.UNMEASURED
    states = {_measurement(costs[event.id].cost_measurement) for event in events}
    if MeasurementState.UNMEASURED in states:
        return None, MeasurementState.UNMEASURED
    total = sum((costs[event.id].total for event in events), Decimal("0"))
    state = (
        MeasurementState.ESTIMATED
        if MeasurementState.ESTIMATED in states
        else MeasurementState.MEASURED
    )
    return total, state


def _source_fingerprint(
    tenant_id: str, executions: list[ExecutionEvent], outcomes: list[OutcomeEvent],
    cost_revisions: list | None = None,
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
    if any(row.maturity not in {None, "unknown"} for row in outcomes):
        version = "stored-assertions/4"
    if cost_revisions:
        version = "stored-assertions/5"
    execution_assertions = [_execution_identity_fields(row) for row in executions]
    if version not in {"stored-assertions/3", "stored-assertions/4", "stored-assertions/5"}:
        for assertion in execution_assertions:
            assertion.pop("cost_role")
            assertion.pop("charge_id")
    if version == "stored-assertions/1":
        # Keep historical v1 bytes stable when the new field carries no assertion.
        for assertion in execution_assertions:
            assertion.pop("source_window_id")
    outcome_assertions = [_outcome_assertions(row) for row in outcomes]
    if version not in {"stored-assertions/4", "stored-assertions/5"}:
        for assertion in outcome_assertions:
            assertion.pop("maturity")
    return EvidenceFingerprint(
        version=version,
        digest=digest({
            "version": version,
            "tenant_id": tenant_id,
            "executions": sorted(digest(assertion) for assertion in execution_assertions),
            "outcomes": sorted(digest(assertion) for assertion in outcome_assertions),
            **({"cost_revisions": sorted(digest(revision_assertions(row)) for row in cost_revisions)}
               if cost_revisions else {}),
        }),
        execution_records=len(executions), outcome_records=len(outcomes),
        cost_revision_records=len(cost_revisions or []),
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
    effective_costs, cost_revisions = resolve_costs(db, executions)
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
        cost, cost_measurement = _run_cost([] if run_id in incomplete_runs else run_events, effective_costs)
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
            cost_revisions,
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


def _stored_probabilistic_decision(
    record: ProbabilisticMigrationDecisionRecord,
) -> ProbabilisticMigrationDecision:
    evaluated_at = record.evaluated_at
    if evaluated_at.tzinfo is None:
        evaluated_at = evaluated_at.replace(tzinfo=UTC)
    return ProbabilisticMigrationDecision.model_validate(record.report_json).model_copy(
        update={"decision_id": record.decision_id, "evaluated_at": evaluated_at}
    )


def evaluate_and_retain_probabilistic_migration(
    db: ScopedSession,
    request: ProbabilisticMigrationRequest,
    *,
    evaluated_by: str,
    evidence_lineage: dict[str, object] | None = None,
    qualification_applicability: QualificationApplicabilityV1 | None = None,
    qualification_checked_at: datetime | None = None,
    qualification_resolution_enabled: bool = True,
) -> ProbabilisticMigrationDecision:
    """Evaluate one immutable paired-evidence snapshot and retain its decision."""
    if type(db) is not ScopedSession or db.scope is None:
        raise TypeError("probabilistic migration decisions require a tenant-scoped session")
    readiness = assess_forecast_readiness(
        request.calibration_observations,
        required_metrics=_MODEL_MIGRATION_CALIBRATION_METRICS,
    )
    derived_evidence = request.evidence.model_copy(update={"readiness": readiness})
    qualification_resolution: QualificationResolution | None = None
    if request.qualification_id is not None or qualification_applicability is not None:
        if qualification_applicability is None:
            qualification_resolution = QualificationResolution(
                status="abstain",
                reason_code="qualification_incomplete",
            )
        else:
            policy_digest = hashlib.sha256(
                json.dumps(
                    request.policy.model_dump(mode="json"),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            request_scope_mismatch = next(
                (
                    reason
                    for failed, reason in (
                        (
                            qualification_applicability.workload != request.evidence.workload,
                            "qualification_workload_scope_mismatch",
                        ),
                        (
                            (
                                qualification_applicability.incumbent_model,
                                qualification_applicability.candidate_model,
                            )
                            != (
                                request.evidence.incumbent_model,
                                request.evidence.candidate_model,
                            ),
                            "qualification_model_pair_scope_mismatch",
                        ),
                        (
                            qualification_applicability.policy_digest != policy_digest,
                            "qualification_policy_scope_mismatch",
                        ),
                        (
                            qualification_applicability.algorithm_version
                            != FORECAST_ALGORITHM_VERSION,
                            "qualification_algorithm_scope_mismatch",
                        ),
                    )
                    if failed
                ),
                None,
            )
            if request_scope_mismatch is not None:
                qualification_resolution = QualificationResolution(
                    status="abstain", reason_code=request_scope_mismatch
                )
            else:
                qualification_resolution = resolve_qualification(
                    db,
                    qualification_applicability,
                    qualification_id=request.qualification_id,
                    checked_at=qualification_checked_at or datetime.now(UTC),
                    resolution_enabled=qualification_resolution_enabled,
                )
    request_json = {
        "evidence": derived_evidence.model_dump(mode="json"),
        "forecast_algorithm_version": FORECAST_ALGORITHM_VERSION,
        "policy": request.policy.model_dump(mode="json"),
        "calibration_observations": [
            observation.model_dump(mode="json")
            for observation in request.calibration_observations
        ],
        "simulations": request.simulations,
        "seed": request.seed,
    }
    if request.qualification_id is not None:
        request_json["qualification_id"] = request.qualification_id
    if qualification_resolution is not None:
        request_json["qualification_resolution"] = qualification_resolution.model_dump(
            mode="json"
        )
    if evidence_lineage is not None:
        request_json["evidence_lineage"] = evidence_lineage
    request_digest = hashlib.sha256(
        json.dumps(request_json, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    decision_id = (
        "pdec_"
        + hashlib.sha256(f"{db.scope.tenant_id}:{request_digest}".encode()).hexdigest()[:24]
    )
    existing = db.get(ProbabilisticMigrationDecisionRecord, decision_id)
    if existing is not None:
        return _stored_probabilistic_decision(existing)

    decision = recommend_model_migration(
        derived_evidence,
        policy=request.policy,
        simulations=request.simulations,
        seed=request.seed,
    )
    now = datetime.now(UTC)
    combined_lineage = {
        **(evidence_lineage or {"kind": "client_snapshot"}),
        **decision.evidence_lineage,
    }
    if qualification_resolution is not None:
        combined_lineage["qualification_resolution"] = qualification_resolution.model_dump(
            mode="json"
        )
    decision = decision.model_copy(update={"evidence_lineage": combined_lineage})
    report_json = decision.model_dump(mode="json", exclude={"decision_id", "evaluated_at"})
    record = ProbabilisticMigrationDecisionRecord(
        decision_id=decision_id,
        tenant_id=db.scope.tenant_id,
        request_digest=request_digest,
        workload=request.evidence.workload,
        incumbent_model=request.evidence.incumbent_model,
        candidate_model=request.evidence.candidate_model,
        verdict=decision.verdict,
        recommended_action=decision.recommended_action,
        evidence_json=derived_evidence.model_dump(mode="json"),
        evidence_lineage_json=combined_lineage,
        policy_json=request.policy.model_dump(mode="json"),
        report_json=report_json,
        evaluated_at=now,
        evaluated_by=evaluated_by,
    )
    db.add(record)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        concurrent = db.get(ProbabilisticMigrationDecisionRecord, decision_id)
        if concurrent is None:
            raise
        return _stored_probabilistic_decision(concurrent)
    return decision.model_copy(update={"decision_id": decision_id, "evaluated_at": now})


@dataclass
class _RetainedCalibrationEvidence:
    observations: list[ForecastCalibrationObservation]
    selected_rows: int
    quarantined: list[dict[str, object]]

    @property
    def gaps(self) -> list[str]:
        return sorted({str(row["reason"]) for row in self.quarantined})


def _retained_calibration_evidence_for_source(
    db: ScopedSession, source: MigrationEvidenceSource
) -> _RetainedCalibrationEvidence:
    rows = list(
        db.scalars(
            select(ForecastCalibrationRecord)
            .where(
                ForecastCalibrationRecord.workload == source.workload,
                ForecastCalibrationRecord.incumbent_model == source.incumbent_model,
                ForecastCalibrationRecord.candidate_model == source.candidate_model,
            )
            .order_by(ForecastCalibrationRecord.observed_at.desc(), ForecastCalibrationRecord.id.desc())
            .limit(200)
        )
    )
    observations = []
    seen: dict[tuple[str, str], tuple[int, ForecastCalibrationObservation]] = {}
    quarantined: dict[int, str] = {}
    verifications = {
        row.verification_id: row
        for row in db.scalars(
            select(RandomizedRolloutVerificationRecord).where(
                RandomizedRolloutVerificationRecord.verification_id.in_(
                    {row.verification_id for row in rows}
                )
            )
        )
    }
    heterogeneous_rollouts = {
        row.rollout_id
        for row in db.scalars(
            select(RandomizedRolloutRecord).where(
                RandomizedRolloutRecord.rollout_id.in_(
                    {row.rollout_id for row in verifications.values()}
                )
            )
        )
        if len({row.candidate_probability, *row.cohort_probabilities_json.values()}) > 1
    }
    # Select first, then validate. Never backfill rejected recent rows with older
    # favorable observations. Quarantine is recorded, not a destructive DB edit.
    for row in reversed(rows):
        verification = verifications.get(row.verification_id)
        if verification is not None and verification.rollout_id in heterogeneous_rollouts:
            quarantined[row.id] = "heterogeneous_assignment_probability"
            continue
        try:
            observation = ForecastCalibrationObservation(
                forecast_id=row.forecast_id,
                metric=row.metric,
                predicted_mean=row.predicted_mean,
                predicted_low=row.predicted_low,
                predicted_high=row.predicted_high,
                observed=row.observed,
                observed_at=(
                    row.observed_at.replace(tzinfo=UTC)
                    if row.observed_at.tzinfo is None
                    else row.observed_at
                ),
            )
        except ValidationError:
            quarantined[row.id] = "invalid_retained_calibration"
            continue
        key = (observation.metric, observation.forecast_id)
        prior = seen.get(key)
        if prior is not None and prior[1] != observation:
            quarantined[prior[0]] = "conflicting_retained_calibration"
            quarantined[row.id] = "conflicting_retained_calibration"
        else:
            seen[key] = (row.id, observation)
        observations.append(observation)
    return _RetainedCalibrationEvidence(
        observations=[] if quarantined else observations,
        selected_rows=len(rows),
        quarantined=[
            {"row_id": row_id, "reason": reason}
            for row_id, reason in sorted(quarantined.items())
        ],
    )


def _calibration_observations_for_source(
    db: ScopedSession, source: MigrationEvidenceSource
) -> list[ForecastCalibrationObservation]:
    return _retained_calibration_evidence_for_source(db, source).observations


def evaluate_probabilistic_migration_from_store(
    db: ScopedSession,
    payload: MigrationEvidenceRefreshRequest,
    *,
    evaluated_by: str,
    now: datetime | None = None,
) -> ProbabilisticMigrationDecision:
    harvested = harvest_migration_evidence(db, payload.evidence_source, now=now)
    calibration = _retained_calibration_evidence_for_source(db, payload.evidence_source)
    lineage = harvested.lineage.model_dump(mode="json")
    lineage["calibration_evidence"] = {
        "selection": "latest_observed_at_then_id",
        "selected_rows": calibration.selected_rows,
        "quarantined": calibration.quarantined,
    }
    gaps = harvested.gaps + calibration.gaps
    if harvested.evidence is not None and not calibration.gaps:
        return evaluate_and_retain_probabilistic_migration(
            db,
            ProbabilisticMigrationRequest(
                evidence=harvested.evidence,
                policy=payload.policy,
                calibration_observations=calibration.observations,
                simulations=payload.simulations,
                seed=payload.seed,
            ),
            evaluated_by=evaluated_by,
            evidence_lineage=lineage,
        )
    decision = ProbabilisticMigrationDecision(
        workload=payload.evidence_source.workload,
        incumbent_model=payload.evidence_source.incumbent_model,
        candidate_model=payload.evidence_source.candidate_model,
        verdict="abstain",
        recommended_action="collect_evidence",
        recommended_candidate_share=0,
        reason_codes=gaps,
        simulations=payload.simulations,
        seed=payload.seed,
        actions=[],
        forecast_readiness=assess_forecast_readiness(
            calibration.observations,
            required_metrics=_MODEL_MIGRATION_CALIBRATION_METRICS,
        ),
        evidence_lineage=lineage,
    )
    digest_payload = {
        "source": payload.evidence_source.model_dump(mode="json"),
        "policy": payload.policy.model_dump(mode="json"),
        "lineage": lineage,
        "gaps": gaps,
        "simulations": payload.simulations,
        "seed": payload.seed,
    }
    digest = hashlib.sha256(
        json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    decision_id = f"pdec_{hashlib.sha256(f'{db.scope.tenant_id}:{digest}'.encode()).hexdigest()[:24]}"
    existing = db.get(ProbabilisticMigrationDecisionRecord, decision_id)
    if existing is not None:
        return _stored_probabilistic_decision(existing)
    evaluated_at = now or datetime.now(UTC)
    db.add(
        ProbabilisticMigrationDecisionRecord(
            decision_id=decision_id,
            tenant_id=db.scope.tenant_id,
            request_digest=digest,
            workload=payload.evidence_source.workload,
            incumbent_model=payload.evidence_source.incumbent_model,
            candidate_model=payload.evidence_source.candidate_model,
            verdict=decision.verdict,
            recommended_action=decision.recommended_action,
            evidence_json={"gaps": gaps},
            evidence_lineage_json=lineage,
            policy_json=payload.policy.model_dump(mode="json"),
            report_json=decision.model_dump(mode="json", exclude={"decision_id", "evaluated_at"}),
            evaluated_at=evaluated_at,
            evaluated_by=evaluated_by,
        )
    )
    db.commit()
    return decision.model_copy(update={"decision_id": decision_id, "evaluated_at": evaluated_at})


def list_probabilistic_migration_decisions(
    db: ScopedSession,
    *,
    workload: str | None = None,
    limit: int = 50,
) -> list[ProbabilisticMigrationDecision]:
    if type(db) is not ScopedSession:
        raise TypeError("probabilistic decision history requires a ScopedSession")
    statement = select(ProbabilisticMigrationDecisionRecord)
    if workload is not None:
        statement = statement.where(ProbabilisticMigrationDecisionRecord.workload == workload)
    records = list(
        db.scalars(
            statement.order_by(
                ProbabilisticMigrationDecisionRecord.evaluated_at.desc()
            ).limit(max(1, min(limit, 200)))
        )
    )
    return [_stored_probabilistic_decision(record) for record in records]


def _rollout_out(record: RandomizedRolloutRecord) -> RandomizedRolloutOut:
    created_at = record.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return RandomizedRolloutOut(
        rollout_id=record.rollout_id,
        decision_id=record.decision_id,
        workload=record.workload,
        incumbent_model=record.incumbent_model,
        candidate_model=record.candidate_model,
        candidate_probability=record.candidate_probability,
        cohort_candidate_probabilities=record.cohort_probabilities_json,
        minimum_per_arm=record.minimum_per_arm,
        active=record.active,
        created_at=created_at,
    )


def create_randomized_rollout(
    db: ScopedSession,
    payload: RandomizedRolloutCreate,
    *,
    created_by: str,
    now: datetime | None = None,
) -> RandomizedRolloutOut:
    if type(db) is not ScopedSession or db.scope is None:
        raise TypeError("randomized rollouts require a tenant-scoped session")
    decision_record = db.get(ProbabilisticMigrationDecisionRecord, payload.decision_id)
    if decision_record is None:
        raise ValueError("probabilistic decision not found")
    decision = _stored_probabilistic_decision(decision_record)
    if decision.verdict != "recommend":
        raise ValueError("only a recommended decision can start a randomized rollout")
    for cohort, probability in payload.cohort_candidate_probabilities.items():
        if not cohort or probability <= 0 or probability >= 1:
            raise ValueError("cohort rollout probabilities must be greater than 0 and less than 1")
    digest = hashlib.sha256(
        f"{db.scope.tenant_id}:{payload.decision_id}:{json.dumps(payload.model_dump(mode='json'), sort_keys=True)}".encode()
    ).hexdigest()
    rollout_id = f"roll_{digest[:24]}"
    existing = db.get(RandomizedRolloutRecord, rollout_id)
    if existing is not None:
        return _rollout_out(existing)
    current = now or datetime.now(UTC)
    record = RandomizedRolloutRecord(
        rollout_id=rollout_id,
        tenant_id=db.scope.tenant_id,
        decision_id=payload.decision_id,
        workload=decision.workload,
        incumbent_model=decision.incumbent_model,
        candidate_model=decision.candidate_model,
        candidate_probability=payload.candidate_probability,
        cohort_probabilities_json=payload.cohort_candidate_probabilities,
        assignment_salt=secrets.token_hex(32),
        minimum_per_arm=payload.minimum_per_arm,
        active=True,
        created_at=current,
        created_by=created_by,
    )
    db.add(record)
    db.commit()
    return _rollout_out(record)


def stop_randomized_rollout(
    db: ScopedSession,
    rollout_id: str,
) -> RandomizedRolloutOut:
    """Idempotently stop one rollout in the bound tenant."""
    if type(db) is not ScopedSession or db.scope is None:
        raise TypeError("randomized rollouts require a tenant-scoped session")
    rollout = db.get(RandomizedRolloutRecord, rollout_id)
    if rollout is None:
        raise ValueError("randomized rollout not found")
    if rollout.active:
        rollout.active = False
        db.commit()
    return _rollout_out(rollout)


def _rollout_plan(record: RandomizedRolloutRecord) -> RandomizedRolloutPlan:
    return RandomizedRolloutPlan(
        rollout_id=record.rollout_id,
        workload=record.workload,
        incumbent_model=record.incumbent_model,
        candidate_model=record.candidate_model,
        candidate_probability=record.candidate_probability,
        cohort_candidate_probabilities=record.cohort_probabilities_json,
        assignment_salt=record.assignment_salt,
    )


def _assignment_out(
    record: RandomizedRolloutAssignmentRecord,
) -> RandomizedRolloutAssignmentOut:
    assigned_at = record.assigned_at
    if assigned_at.tzinfo is None:
        assigned_at = assigned_at.replace(tzinfo=UTC)
    return RandomizedRolloutAssignmentOut(
        rollout_id=record.rollout_id,
        subject_id=record.subject_id,
        arm=record.arm,
        assigned_model=record.assigned_model,
        assigned_at=assigned_at,
    )


def assign_randomized_rollout(
    db: ScopedSession,
    rollout_id: str,
    *,
    subject_id: str,
    cohort: str = "default",
    now: datetime | None = None,
) -> RandomizedRolloutAssignmentOut:
    if type(db) is not ScopedSession or db.scope is None:
        raise TypeError("rollout assignment requires a tenant-scoped session")
    assignment_id = "rasn_" + hashlib.sha256(
        f"{db.scope.tenant_id}:{rollout_id}:{subject_id}".encode()
    ).hexdigest()[:24]
    rollout = db.get(RandomizedRolloutRecord, rollout_id)
    if rollout is None:
        raise ValueError("randomized rollout not found")
    existing = db.get(RandomizedRolloutAssignmentRecord, assignment_id)
    if existing is not None:
        return _assignment_out(existing)
    if not rollout.active:
        raise RandomizedRolloutInactiveError("randomized rollout is stopped")
    assignment = assign_rollout_arm(
        _rollout_plan(rollout), subject_id, cohort=cohort, assigned_at=now
    )
    record = RandomizedRolloutAssignmentRecord(
        assignment_id=assignment_id,
        tenant_id=db.scope.tenant_id,
        rollout_id=rollout_id,
        subject_id=subject_id,
        cohort=cohort,
        arm=assignment.arm,
        assigned_model=assignment.assigned_model,
        assigned_at=assignment.assigned_at,
    )
    db.add(record)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        concurrent = db.get(RandomizedRolloutAssignmentRecord, assignment_id)
        if concurrent is None:
            raise
        return _assignment_out(concurrent)
    return _assignment_out(record)


def _rollout_observations_from_store(
    db: ScopedSession,
    rollout: RandomizedRolloutRecord,
    assignments: list[RandomizedRolloutAssignmentRecord],
    *,
    outcome_type: str,
) -> list[RolloutObservation]:
    if not assignments:
        return []
    subjects = [row.subject_id for row in assignments]
    events = list(
        db.scalars(
            select(ExecutionEvent).where(
                ExecutionEvent.capability_id == rollout.workload,
                ExecutionEvent.subject_id.in_(subjects),
                ExecutionEvent.timestamp >= rollout.created_at,
            )
        )
    )
    outcomes = list(
        db.scalars(
            select(OutcomeEvent).where(
                OutcomeEvent.capability_id == rollout.workload,
                OutcomeEvent.outcome_type == outcome_type,
                OutcomeEvent.occurred_at >= rollout.created_at,
            )
        )
    )
    outcome_by_join = {row.join_key or row.execution_id: row for row in outcomes}
    effective_costs, _ = resolve_costs(db, events)
    observations: list[RolloutObservation] = []
    for event in events:
        outcome = outcome_by_join.get(event.join_key or event.execution_id)
        accepted = _accepted(outcome)
        cost, measurement = _run_cost([event], effective_costs)
        if outcome is None or accepted is None or cost is None:
            continue
        if measurement is MeasurementState.UNMEASURED:
            continue
        observations.append(
            RolloutObservation(
                subject_id=event.subject_id or "",
                model_used=event.model_version,
                cost_usd=cost,
                latency_ms=event.latency_ms,
                accepted=accepted,
                critical_error=(outcome.outcome_payload_json or {}).get(
                    "critical_error", False
                )
                is True,
                observed_at=(
                    event.timestamp.replace(tzinfo=UTC)
                    if event.timestamp.tzinfo is None
                    else event.timestamp
                ),
            )
        )
    return observations


def _retain_rollout_calibration(
    db: ScopedSession,
    rollout: RandomizedRolloutRecord,
    verification_id: str,
    verification: RolloutVerification,
    observations: list[RolloutObservation],
) -> None:
    if verification.causal_status != "verified" or not observations:
        return
    decision_record = db.get(ProbabilisticMigrationDecisionRecord, rollout.decision_id)
    assert decision_record is not None
    decision = _stored_probabilistic_decision(decision_record)
    action = min(
        decision.actions,
        key=lambda row: abs(row.candidate_share - rollout.candidate_probability),
    )
    calibration_fields = (
        action.expected_success_rate,
        action.success_rate_p05,
        action.success_rate_p95,
        action.expected_p95_latency_ms,
        action.p95_latency_p05_ms,
        action.p95_latency_p95_ms,
        action.expected_critical_error_rate,
        action.critical_error_rate_p05,
        action.critical_error_rate_p95,
    )
    if any(value is None for value in calibration_fields):
        return
    evidence = decision_record.evidence_json
    demand = fmean(evidence["period_request_counts"])
    costs = [float(row.cost_usd) for row in observations]
    latencies = sorted(row.latency_ms for row in observations)
    observed_values = {
        "monthly_cost_usd": fmean(costs) * demand,
        "success_rate": fmean(row.accepted for row in observations),
        "p95_latency_ms": float(latencies[max(0, ceil(0.95 * len(latencies)) - 1)]),
        "critical_error_rate": fmean(row.critical_error for row in observations),
    }
    predictions = {
        "monthly_cost_usd": (
            float(action.expected_monthly_cost_usd),
            float(action.monthly_cost_p05_usd),
            float(action.monthly_cost_p95_usd),
        ),
        "success_rate": (
            action.expected_success_rate,
            action.success_rate_p05,
            action.success_rate_p95,
        ),
        "p95_latency_ms": (
            action.expected_p95_latency_ms,
            action.p95_latency_p05_ms,
            action.p95_latency_p95_ms,
        ),
        "critical_error_rate": (
            action.expected_critical_error_rate,
            action.critical_error_rate_p05,
            action.critical_error_rate_p95,
        ),
    }
    for metric, (mean, low, high) in predictions.items():
        db.add(
            ForecastCalibrationRecord(
                tenant_id=db.scope.tenant_id,
                forecast_id=f"{rollout.decision_id}:{metric}",
                verification_id=verification_id,
                workload=rollout.workload,
                incumbent_model=rollout.incumbent_model,
                candidate_model=rollout.candidate_model,
                metric=metric,
                predicted_mean=mean,
                predicted_low=low,
                predicted_high=high,
                observed=observed_values[metric],
                observed_at=verification.verified_at,
            )
        )


def verify_retained_randomized_rollout(
    db: ScopedSession,
    rollout_id: str,
    *,
    outcome_type: str = "accepted",
    bootstrap_samples: int = 2_000,
    seed: int = 7,
    now: datetime | None = None,
    verified_by: str = "system",
) -> RolloutVerification:
    if type(db) is not ScopedSession or db.scope is None:
        raise TypeError("rollout verification requires a tenant-scoped session")
    rollout = db.get(RandomizedRolloutRecord, rollout_id)
    if rollout is None:
        raise ValueError("randomized rollout not found")
    assignments = list(
        db.scalars(
            select(RandomizedRolloutAssignmentRecord).where(
                RandomizedRolloutAssignmentRecord.rollout_id == rollout_id
            )
        )
    )
    observations = _rollout_observations_from_store(
        db, rollout, assignments, outcome_type=outcome_type
    )
    domain_assignments = [
        RolloutAssignment(
            subject_id=row.subject_id,
            arm=row.arm,
            assigned_model=row.assigned_model,
            assigned_at=(
                row.assigned_at.replace(tzinfo=UTC)
                if row.assigned_at.tzinfo is None
                else row.assigned_at
            ),
        )
        for row in assignments
    ]
    verified_at = now or datetime.now(UTC)
    digest_payload = {
        "rollout_id": rollout_id,
        "verification_algorithm_version": _ROLLOUT_VERIFICATION_ALGORITHM_VERSION,
        "assignment_probabilities": {
            "default": rollout.candidate_probability,
            "cohorts": rollout.cohort_probabilities_json,
        },
        "outcome_type": outcome_type,
        "bootstrap_samples": bootstrap_samples,
        "seed": seed,
        "assignments": [row.model_dump(mode="json") for row in domain_assignments],
        "observations": [row.model_dump(mode="json") for row in observations],
    }
    request_digest = hashlib.sha256(
        json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    verification_id = f"rver_{request_digest[:24]}"
    existing = db.get(RandomizedRolloutVerificationRecord, verification_id)
    if existing is not None:
        stored = RolloutVerification.model_validate(existing.report_json)
        return stored.model_copy(update={"verification_id": verification_id})
    verification = verify_randomized_rollout(
        _rollout_plan(rollout),
        domain_assignments,
        observations,
        minimum_per_arm=rollout.minimum_per_arm,
        bootstrap_samples=bootstrap_samples,
        seed=seed,
        verified_at=verified_at,
    ).model_copy(update={"verification_id": verification_id})
    assignments_by_subject = {row.subject_id: row for row in domain_assignments}
    compliant_observations: dict[str, RolloutObservation] = {}
    for observation in sorted(observations, key=lambda row: row.observed_at):
        assignment = assignments_by_subject.get(observation.subject_id)
        if (
            assignment is not None
            and observation.observed_at >= assignment.assigned_at
            and observation.model_used == assignment.assigned_model
        ):
            compliant_observations.setdefault(observation.subject_id, observation)
    db.add(
        RandomizedRolloutVerificationRecord(
            verification_id=verification_id,
            tenant_id=db.scope.tenant_id,
            rollout_id=rollout_id,
            request_digest=request_digest,
            report_json=verification.model_dump(mode="json"),
            verified_at=verified_at,
            verified_by=verified_by,
        )
    )
    _retain_rollout_calibration(
        db,
        rollout,
        verification_id,
        verification,
        list(compliant_observations.values()),
    )
    db.commit()
    return verification


def _probabilistic_schedule_out(
    schedule: ProbabilisticDecisionSchedule,
) -> ProbabilisticDecisionScheduleOut:
    def utc(value: datetime | None) -> datetime | None:
        if value is None or value.tzinfo is not None:
            return value
        return value.replace(tzinfo=UTC)

    return ProbabilisticDecisionScheduleOut.model_validate(
        {
            "schedule_id": schedule.schedule_id,
            "evidence_source": schedule.evidence_source_json,
            "policy": schedule.policy_json,
            "interval_minutes": schedule.interval_minutes,
            "simulations": schedule.simulations,
            "seed": schedule.seed,
            "active": schedule.active,
            "next_run_at": utc(schedule.next_run_at),
            "last_run_at": utc(schedule.last_run_at),
            "last_decision_id": schedule.last_decision_id,
            # Preserve historical storage, but never expose legacy exception text.
            "last_error": _SCHEDULE_FAILURE_CODE if schedule.last_error is not None else None,
            "created_at": utc(schedule.created_at),
        }
    )


def create_probabilistic_decision_schedule(
    db: ScopedSession,
    payload: ProbabilisticDecisionScheduleCreate,
    *,
    created_by: str,
    now: datetime | None = None,
) -> ProbabilisticDecisionScheduleOut:
    if type(db) is not ScopedSession or db.scope is None:
        raise TypeError("probabilistic schedules require a tenant-scoped session")
    import uuid

    current = now or datetime.now(UTC)
    schedule = ProbabilisticDecisionSchedule(
        schedule_id=f"psch_{uuid.uuid4().hex[:24]}",
        tenant_id=db.scope.tenant_id,
        evidence_source_json=payload.evidence_source.model_dump(mode="json"),
        policy_json=payload.policy.model_dump(mode="json"),
        simulations=payload.simulations,
        seed=payload.seed,
        interval_minutes=payload.interval_minutes,
        active=True,
        next_run_at=current,
        last_run_at=None,
        last_decision_id=None,
        last_error=None,
        created_at=current,
        updated_at=current,
        created_by=created_by,
    )
    db.add(schedule)
    db.commit()
    return _probabilistic_schedule_out(schedule)


def list_probabilistic_decision_schedules(
    db: ScopedSession,
) -> list[ProbabilisticDecisionScheduleOut]:
    if type(db) is not ScopedSession:
        raise TypeError("probabilistic schedules require a ScopedSession")
    rows = list(
        db.scalars(
            select(ProbabilisticDecisionSchedule).order_by(
                ProbabilisticDecisionSchedule.created_at.desc()
            )
        )
    )
    return [_probabilistic_schedule_out(row) for row in rows]


def deactivate_probabilistic_decision_schedule(
    db: ScopedSession,
    schedule_id: str,
    *,
    now: datetime | None = None,
) -> ProbabilisticDecisionScheduleOut:
    """Idempotently deactivate one probabilistic schedule in the bound tenant."""
    if type(db) is not ScopedSession or db.scope is None:
        raise TypeError("probabilistic schedules require a tenant-scoped session")
    schedule = db.get(ProbabilisticDecisionSchedule, schedule_id)
    if schedule is None:
        raise ValueError("probabilistic decision schedule not found")
    if schedule.active:
        schedule.active = False
        schedule.updated_at = now or datetime.now(UTC)
        db.commit()
    return _probabilistic_schedule_out(schedule)


def run_due_probabilistic_decision_schedules(
    db: ScopedSession,
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> list[ProbabilisticMigrationDecision]:
    if type(db) is not ScopedSession:
        raise TypeError("probabilistic schedules require a ScopedSession")
    current = now or datetime.now(UTC)
    due = list(
        db.scalars(
            select(ProbabilisticDecisionSchedule)
            .where(
                ProbabilisticDecisionSchedule.active.is_(True),
                ProbabilisticDecisionSchedule.next_run_at <= current,
            )
            .order_by(ProbabilisticDecisionSchedule.next_run_at)
            .limit(max(1, min(limit, 500)))
        )
    )
    completed: list[ProbabilisticMigrationDecision] = []
    for due_schedule in due:
        next_run = current + timedelta(minutes=due_schedule.interval_minutes)
        claimed = db.execute(
            update(ProbabilisticDecisionSchedule)
            .where(
                ProbabilisticDecisionSchedule.schedule_id == due_schedule.schedule_id,
                ProbabilisticDecisionSchedule.active.is_(True),
                ProbabilisticDecisionSchedule.next_run_at <= current,
            )
            .values(next_run_at=next_run, updated_at=current)
            .returning(ProbabilisticDecisionSchedule.schedule_id)
            .execution_options(synchronize_session=False)
        ).scalar_one_or_none()
        db.commit()
        if claimed is None:
            continue
        schedule = db.get(ProbabilisticDecisionSchedule, claimed)
        assert schedule is not None
        reserved = False
        try:
            reserved = reserve_usage(db, "decision_scans")
            source = MigrationEvidenceSource.model_validate(schedule.evidence_source_json)
            decision = evaluate_probabilistic_migration_from_store(
                db,
                MigrationEvidenceRefreshRequest(
                    evidence_source=source,
                    policy=schedule.policy_json,
                    simulations=schedule.simulations,
                    seed=schedule.seed,
                ),
                evaluated_by=f"schedule:{schedule.schedule_id}",
                now=current,
            )
            schedule = db.get(ProbabilisticDecisionSchedule, claimed)
            assert schedule is not None
            schedule.last_run_at = current
            schedule.last_decision_id = decision.decision_id
            schedule.last_error = None
            schedule.updated_at = current
            db.commit()
            completed.append(decision)
        except Exception as exc:  # noqa: BLE001
            logger.error("probabilistic schedule failed exception_type=%s", type(exc).__name__)
            db.rollback()
            if reserved:
                release_usage(db, "decision_scans")
            schedule = db.get(ProbabilisticDecisionSchedule, claimed)
            if schedule is not None:
                schedule.last_run_at = current
                schedule.last_error = _SCHEDULE_FAILURE_CODE
                schedule.updated_at = current
                db.commit()
    return completed


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
            # Preserve historical storage, but never expose legacy exception text.
            "last_error": _SCHEDULE_FAILURE_CODE if schedule.last_error is not None else None,
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


def deactivate_decision_schedule(
    db: ScopedSession,
    schedule_id: str,
    *,
    now: datetime | None = None,
) -> DecisionScheduleOut:
    """Idempotently deactivate one deterministic schedule in the bound tenant."""
    if type(db) is not ScopedSession or db.scope is None:
        raise TypeError("decision schedules require a tenant-scoped session")
    schedule = db.get(DecisionSchedule, schedule_id)
    if schedule is None:
        raise ValueError("decision schedule not found")
    if schedule.active:
        schedule.active = False
        schedule.updated_at = now or datetime.now(UTC)
        db.commit()
    return _schedule_out(schedule)


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
            logger.error("deterministic schedule failed exception_type=%s", type(exc).__name__)
            db.rollback()
            if reserved:
                release_usage(db, "decision_scans")
            schedule = db.get(DecisionSchedule, due_schedule.schedule_id)
            if schedule is not None:
                schedule.last_run_at = current
                schedule.last_error = _SCHEDULE_FAILURE_CODE
                schedule.updated_at = current
                db.commit()
    return completed
