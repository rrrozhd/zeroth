"""Build auditable probabilistic migration evidence from tenant telemetry."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from zeroth.econ.probabilistic import MigrationEvidence, MigrationObservation
from zeroth.econ.plane.backtesting.models import EconomicBacktestRecord
from zeroth.econ.plane.decisioning.schemas import MigrationEvidenceSource
from zeroth.econ.plane.instrumentation.models import ExecutionEvent, OutcomeEvent
from zeroth.econ.plane.scoped_session import ScopedSession


class EvidenceLineage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    collected_at: datetime
    lookback_start: datetime
    paired_cases: int = Field(ge=0)
    incumbent_complete_cases: int = Field(ge=0)
    candidate_complete_cases: int = Field(ge=0)
    excluded_incomplete_cases: int = Field(ge=0)
    sources: list[str]


class EvidenceHarvestResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence: MigrationEvidence | None = None
    gaps: list[str]
    lineage: EvidenceLineage


def _accepted(outcome: OutcomeEvent) -> bool | None:
    value = (outcome.outcome_payload_json or {}).get("accepted")
    if type(value) is bool:
        return value
    raw = outcome.outcome_value.strip().lower()
    if raw in {"true", "1", "yes", "accepted", "success"}:
        return True
    if raw in {"false", "0", "no", "rejected", "failure"}:
        return False
    return None


def _critical(outcome: OutcomeEvent) -> bool | None:
    value = (outcome.outcome_payload_json or {}).get("critical_error")
    return value if type(value) is bool else None


def harvest_migration_evidence(
    db: ScopedSession,
    source: MigrationEvidenceSource,
    *,
    now: datetime | None = None,
) -> EvidenceHarvestResult:
    """Collect complete, paired case evidence without inventing missing samples."""

    if type(db) is not ScopedSession:
        raise TypeError("migration evidence harvesting requires a ScopedSession")
    collected_at = now or datetime.now(UTC)
    if collected_at.tzinfo is None:
        collected_at = collected_at.replace(tzinfo=UTC)
    lookback_start = collected_at - timedelta(days=source.lookback_days)
    events = list(
        db.scalars(
            select(ExecutionEvent).where(
                ExecutionEvent.capability_id == source.workload,
                ExecutionEvent.model_version.in_(
                    [source.incumbent_model, source.candidate_model]
                ),
                ExecutionEvent.timestamp >= lookback_start,
                ExecutionEvent.timestamp <= collected_at,
            )
        )
    )
    outcomes = list(
        db.scalars(
            select(OutcomeEvent).where(
                OutcomeEvent.capability_id == source.workload,
                OutcomeEvent.outcome_type == source.outcome_type,
                OutcomeEvent.occurred_at >= lookback_start,
                OutcomeEvent.occurred_at <= collected_at,
            )
        )
    )
    outcome_by_join = {row.join_key or row.execution_id: row for row in outcomes}
    grouped: dict[tuple[str, str], list[ExecutionEvent]] = defaultdict(list)
    for event in events:
        case_id = event.subject_id or event.join_key or event.execution_id
        grouped[(event.model_version, case_id)].append(event)

    observations: dict[str, dict[str, MigrationObservation]] = {
        source.incumbent_model: {},
        source.candidate_model: {},
    }
    case_dates: dict[str, date] = {}
    excluded = 0
    sources: set[str] = set()
    for (model, case_id), run_events in grouped.items():
        if any(event.cost_measurement.lower() == "unmeasured" for event in run_events):
            excluded += 1
            continue
        matching_outcomes = [
            outcome_by_join.get(event.join_key or event.execution_id) for event in run_events
        ]
        complete_outcomes = [row for row in matching_outcomes if row is not None]
        outcome = max(complete_outcomes, key=lambda row: row.occurred_at) if complete_outcomes else None
        accepted = _accepted(outcome) if outcome is not None else None
        if accepted is None:
            excluded += 1
            continue
        dimensions = next((event.dimensions for event in run_events if event.dimensions), {})
        cohort = dimensions.get(source.cohort_dimension, "default")
        if not isinstance(cohort, str) or not cohort:
            cohort = "default"
        evidence_sources = {event.evidence_kind for event in run_events}
        sources.update(evidence_sources)
        cost = sum(
            (
                (event.token_cost_usd or Decimal("0"))
                + (event.tool_cost_usd or Decimal("0"))
                + (event.compute_cost_usd or Decimal("0"))
                for event in run_events
            ),
            Decimal("0"),
        )
        critical = _critical(outcome)
        observations[model][case_id] = MigrationObservation(
            case_id=case_id,
            cohort=cohort,
            cost_usd=cost,
            latency_ms=sum(event.latency_ms for event in run_events),
            accepted=accepted,
            source="+".join(sorted(evidence_sources)),
            **({"critical_error": critical} if critical is not None else {}),
        )
        if model == source.incumbent_model:
            case_dates[case_id] = max(event.timestamp for event in run_events).date()

    incumbent = observations[source.incumbent_model]
    candidate = observations[source.candidate_model]
    paired_ids = sorted(incumbent.keys() & candidate.keys())
    if not paired_ids:
        backtest = db.scalars(
            select(EconomicBacktestRecord)
            .where(
                EconomicBacktestRecord.workflow == source.workload,
                EconomicBacktestRecord.incumbent_model == source.incumbent_model,
                EconomicBacktestRecord.candidate_model == source.candidate_model,
                EconomicBacktestRecord.evaluated_at >= lookback_start,
                EconomicBacktestRecord.evaluated_at <= collected_at,
            )
            .order_by(EconomicBacktestRecord.evaluated_at.desc())
            .limit(1)
        ).one_or_none()
        report = backtest.report_json if backtest is not None else {}
        incumbent_artifact = report.get("incumbent_observations")
        candidate_artifact = report.get("candidate_observations")
        period_artifact = report.get("period_request_counts")
        # Aggregate-only backtests retain empty defaults. They are missing
        # evidence, not a malformed nonempty artifact to validate as a forecast.
        if (
            isinstance(incumbent_artifact, list)
            and incumbent_artifact
            and isinstance(candidate_artifact, list)
            and candidate_artifact
            and isinstance(period_artifact, list)
            and period_artifact
        ):
            artifact_evidence = MigrationEvidence.model_validate(
                {
                    "workload": source.workload,
                    "incumbent_model": source.incumbent_model,
                    "candidate_model": source.candidate_model,
                    "incumbent": incumbent_artifact,
                    "candidate": candidate_artifact,
                    "period_request_counts": period_artifact,
                    "demand_horizon": report.get("demand_horizon", "unknown"),
                }
            )
            artifact_sources = sorted(
                {row.source for row in artifact_evidence.incumbent}
                | {row.source for row in artifact_evidence.candidate}
            )
            artifact_pairs = len(
                {row.case_id for row in artifact_evidence.incumbent}
                & {row.case_id for row in artifact_evidence.candidate}
            )
            return EvidenceHarvestResult(
                evidence=artifact_evidence,
                gaps=[],
                lineage=EvidenceLineage(
                    collected_at=collected_at,
                    lookback_start=lookback_start,
                    paired_cases=artifact_pairs,
                    incumbent_complete_cases=len(artifact_evidence.incumbent),
                    candidate_complete_cases=len(artifact_evidence.candidate),
                    excluded_incomplete_cases=excluded,
                    sources=artifact_sources,
                ),
            )
    lineage = EvidenceLineage(
        collected_at=collected_at,
        lookback_start=lookback_start,
        paired_cases=len(paired_ids),
        incumbent_complete_cases=len(incumbent),
        candidate_complete_cases=len(candidate),
        excluded_incomplete_cases=excluded,
        sources=sorted(sources),
    )
    if not paired_ids:
        return EvidenceHarvestResult(
            gaps=["no_paired_case_level_evidence"], lineage=lineage
        )
    if incumbent.keys() != candidate.keys():
        return EvidenceHarvestResult(gaps=["paired_outcomes_missing"], lineage=lineage)
    counts: dict[date, int] = defaultdict(int)
    for case_id in incumbent:
        if case_id in case_dates:
            counts[case_dates[case_id]] += 1
    evidence = MigrationEvidence(
        workload=source.workload,
        incumbent_model=source.incumbent_model,
        candidate_model=source.candidate_model,
        incumbent=[incumbent[case_id] for case_id in paired_ids],
        candidate=[candidate[case_id] for case_id in paired_ids],
        period_request_counts=[counts[day] for day in sorted(counts)] or [len(incumbent)],
    )
    return EvidenceHarvestResult(evidence=evidence, gaps=[], lineage=lineage)


__all__ = ["EvidenceHarvestResult", "EvidenceLineage", "harvest_migration_evidence"]
