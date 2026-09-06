"""Evidence-gated economic change decisions for versioned AI workflows.

The module is deliberately independent of persistence and transport. Hosted and
local callers normalize their execution/outcome evidence into ``RunEvidence``
and receive the same pass/fail/abstain decision. A missing or estimated dollar
never becomes a confident approval by default.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from fractions import Fraction
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from zeroth.econ.measurement import MeasurementState


class RunEvidence(BaseModel):
    """Economic and outcome evidence for one end-to-end workflow run."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    cost_usd: Decimal | None = Field(default=None, ge=0)
    cost_measurement: MeasurementState = MeasurementState.UNMEASURED
    accepted: bool | None = None
    outcome_measurement: MeasurementState = MeasurementState.MEASURED

    @model_validator(mode="after")
    def _cost_matches_provenance(self) -> RunEvidence:
        if self.cost_measurement is MeasurementState.UNMEASURED and self.cost_usd is not None:
            raise ValueError("unmeasured cost must not include a value")
        if self.cost_measurement is not MeasurementState.UNMEASURED and self.cost_usd is None:
            raise ValueError("measured or estimated cost requires a value")
        return self


class EvidenceFingerprint(BaseModel):
    """Identity of selected stored assertions; not a completeness guarantee."""

    model_config = ConfigDict(extra="forbid")

    version: Literal["stored-assertions/1"] = "stored-assertions/1"
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_records: int = Field(ge=0)
    outcome_records: int = Field(ge=0)


class VersionEvidence(BaseModel):
    """All in-window run evidence for one exact workflow version."""

    model_config = ConfigDict(extra="forbid")

    workflow: str = Field(min_length=1)
    version: str = Field(min_length=1)
    runs: list[RunEvidence] = Field(default_factory=list)
    source_fingerprint: EvidenceFingerprint | None = None


class DecisionPolicy(BaseModel):
    """Minimum evidence and economic constraints for a version decision."""

    model_config = ConfigDict(extra="forbid")

    min_runs: int = Field(default=10, ge=1)
    min_outcome_coverage: float = Field(default=0.8, ge=0, le=1)
    min_success_rate: float | None = Field(default=None, ge=0, le=1)
    max_success_rate_drop: float = Field(default=0.05, ge=0, le=1)
    max_cost_per_outcome_increase: float = Field(default=0.1, ge=0, allow_inf_nan=False)
    allow_estimated_cost: bool = False
    allow_inferred_outcomes: bool = False


class VersionEconomics(BaseModel):
    """Provenance-aware economics for one workflow version."""

    model_config = ConfigDict(extra="forbid")

    workflow: str
    version: str
    runs: int
    labeled_runs: int
    accepted_runs: int
    rejected_runs: int
    inferred_outcome_runs: int
    outcome_coverage: float
    success_rate: float | None
    measured_cost_usd: Decimal
    estimated_cost_usd: Decimal
    measured_runs: int
    estimated_runs: int
    unmeasured_runs: int
    cost_per_accepted_outcome_usd: Decimal | None


class EconomicDecision(BaseModel):
    """Auditable economic release decision for a candidate workflow version."""

    model_config = ConfigDict(extra="forbid")

    workflow: str
    baseline_version: str
    candidate_version: str
    verdict: Literal["pass", "fail", "abstain"]
    recommended_action: Literal[
        "approve", "review_candidate", "hold", "investigate", "collect_evidence"
    ]
    reason_codes: list[str]
    baseline: VersionEconomics
    candidate: VersionEconomics
    success_rate_change: float | None = None
    cost_per_outcome_change: float | None = None
    policy: DecisionPolicy
    decision_id: str | None = None
    evaluated_at: datetime | None = None
    claim_class: Literal["legacy_unclassified", "observed_comparison"] = "legacy_unclassified"
    method_version: str = "legacy_unversioned"
    limitations: list[str] = Field(default_factory=list)
    source_evidence: dict[Literal["baseline", "candidate"], EvidenceFingerprint] = Field(
        default_factory=dict
    )


def _summarize(evidence: VersionEvidence, *, allow_estimated_cost: bool) -> VersionEconomics:
    runs = len(evidence.runs)
    labeled = [run for run in evidence.runs if run.accepted is not None]
    accepted = sum(run.accepted is True for run in labeled)
    rejected = len(labeled) - accepted
    inferred_outcomes = sum(
        run.outcome_measurement is not MeasurementState.MEASURED for run in labeled
    )
    measured = [run for run in evidence.runs if run.cost_measurement is MeasurementState.MEASURED]
    estimated = [run for run in evidence.runs if run.cost_measurement is MeasurementState.ESTIMATED]
    unmeasured = [
        run for run in evidence.runs if run.cost_measurement is MeasurementState.UNMEASURED
    ]
    measured_cost = sum((run.cost_usd or Decimal("0") for run in measured), Decimal("0"))
    estimated_cost = sum((run.cost_usd or Decimal("0") for run in estimated), Decimal("0"))
    coverage = len(labeled) / runs if runs else 0.0
    success_rate = accepted / len(labeled) if labeled else None

    comparable_cost: Decimal | None = None
    if len(labeled) == runs and not unmeasured and (allow_estimated_cost or not estimated):
        comparable_cost = measured_cost + (estimated_cost if allow_estimated_cost else Decimal("0"))
    cost_per_outcome = (
        comparable_cost / accepted if comparable_cost is not None and accepted else None
    )

    return VersionEconomics(
        workflow=evidence.workflow,
        version=evidence.version,
        runs=runs,
        labeled_runs=len(labeled),
        accepted_runs=accepted,
        rejected_runs=rejected,
        inferred_outcome_runs=inferred_outcomes,
        outcome_coverage=coverage,
        success_rate=success_rate,
        measured_cost_usd=measured_cost,
        estimated_cost_usd=estimated_cost,
        measured_runs=len(measured),
        estimated_runs=len(estimated),
        unmeasured_runs=len(unmeasured),
        cost_per_accepted_outcome_usd=cost_per_outcome,
    )


def _evidence_reasons(
    label: str,
    summary: VersionEconomics,
    policy: DecisionPolicy,
) -> list[str]:
    reasons: list[str] = []
    if summary.runs < policy.min_runs:
        reasons.append(f"{label}_runs_below_minimum")
    coverage = Fraction(summary.labeled_runs, summary.runs) if summary.runs else Fraction(0)
    if coverage < Fraction(str(policy.min_outcome_coverage)):
        reasons.append(f"{label}_outcome_coverage_below_minimum")
    if not policy.allow_inferred_outcomes and summary.inferred_outcome_runs:
        reasons.append(f"{label}_contains_inferred_outcomes")
    if not policy.allow_estimated_cost and summary.estimated_runs:
        reasons.append(f"{label}_contains_estimated_cost")
    if summary.unmeasured_runs:
        reasons.append(f"{label}_contains_unmeasured_cost")
    return reasons


def _cost_per_outcome(summary: VersionEconomics, *, allow_estimated_cost: bool) -> Fraction | None:
    """Use exact totals and counts; a displayed decimal quotient is not a policy input."""
    if summary.cost_per_accepted_outcome_usd is None:
        return None
    total = Fraction(summary.measured_cost_usd)
    if allow_estimated_cost:
        total += Fraction(summary.estimated_cost_usd)
    return total / summary.accepted_runs


def compare_workflow_versions(
    baseline_evidence: VersionEvidence,
    candidate_evidence: VersionEvidence,
    *,
    policy: DecisionPolicy | None = None,
) -> EconomicDecision:
    """Compare a candidate to a baseline without manufacturing confidence.

    ``abstain`` means the evidence contract was not met. ``fail`` means the
    evidence was sufficient and an economic or outcome constraint failed.
    ``pass`` means every declared constraint was satisfied; it does not claim a
    causal effect beyond the supplied evidence window.
    """
    if baseline_evidence.workflow != candidate_evidence.workflow:
        raise ValueError("baseline and candidate must describe the same workflow")
    active_policy = policy or DecisionPolicy()
    claim_fields = {
        "claim_class": "observed_comparison",
        "method_version": "observed-policy/1",
        "limitations": [
            "source_completeness_unverified",
            "no_statistical_causal_or_forecast_authorization",
        ],
        "source_evidence": {
            label: evidence.source_fingerprint
            for label, evidence in (
                ("baseline", baseline_evidence), ("candidate", candidate_evidence)
            )
            if evidence.source_fingerprint is not None
        },
    }
    baseline = _summarize(
        baseline_evidence, allow_estimated_cost=active_policy.allow_estimated_cost
    )
    candidate = _summarize(
        candidate_evidence, allow_estimated_cost=active_policy.allow_estimated_cost
    )

    evidence_reasons = [
        *_evidence_reasons("baseline", baseline, active_policy),
        *_evidence_reasons("candidate", candidate, active_policy),
    ]
    if active_policy.min_success_rate is None:
        evidence_reasons.insert(0, "policy.min_success_rate")
    if baseline.accepted_runs == 0:
        evidence_reasons.append("baseline_has_no_accepted_outcomes")
    baseline_cpo = _cost_per_outcome(
        baseline, allow_estimated_cost=active_policy.allow_estimated_cost,
    )
    candidate_cpo = _cost_per_outcome(
        candidate, allow_estimated_cost=active_policy.allow_estimated_cost,
    )
    if not evidence_reasons and (
        baseline_cpo in {None, 0}
        or (candidate_cpo is None and candidate.accepted_runs > 0)
    ):
        evidence_reasons.append("cost_per_outcome_comparison_unavailable")
    if evidence_reasons:
        return EconomicDecision(
            **claim_fields,
            workflow=baseline.workflow,
            baseline_version=baseline.version,
            candidate_version=candidate.version,
            verdict="abstain",
            recommended_action="collect_evidence",
            reason_codes=evidence_reasons,
            baseline=baseline,
            candidate=candidate,
            policy=active_policy,
        )

    candidate_success = (
        Fraction(candidate.accepted_runs, candidate.labeled_runs)
        if candidate.labeled_runs else None
    )
    baseline_success = (
        Fraction(baseline.accepted_runs, baseline.labeled_runs) if baseline.labeled_runs else None
    )
    exact_success_change = (
        candidate_success - baseline_success
        if candidate_success is not None and baseline_success is not None
        else None
    )
    exact_cost_change = (
        (candidate_cpo - baseline_cpo) / baseline_cpo
        if candidate_cpo is not None and baseline_cpo not in {None, 0}
        else None
    )
    success_change = float(exact_success_change) if exact_success_change is not None else None
    cost_change = float(exact_cost_change) if exact_cost_change is not None else None

    outcome_failures: list[str] = []
    if candidate_success is None or candidate.accepted_runs == 0:
        outcome_failures.append("candidate_has_no_accepted_outcomes")
    else:
        if candidate_success < Fraction(str(active_policy.min_success_rate)):
            outcome_failures.append("candidate_success_rate_below_minimum")
        if (
            exact_success_change is not None
            and exact_success_change < -Fraction(str(active_policy.max_success_rate_drop))
        ):
            outcome_failures.append("candidate_success_rate_drop_exceeds_limit")
    if outcome_failures:
        return EconomicDecision(
            **claim_fields,
            workflow=baseline.workflow,
            baseline_version=baseline.version,
            candidate_version=candidate.version,
            verdict="fail",
            recommended_action="hold",
            reason_codes=outcome_failures,
            baseline=baseline,
            candidate=candidate,
            success_rate_change=success_change,
            cost_per_outcome_change=cost_change,
            policy=active_policy,
        )

    if exact_cost_change is None:
        return EconomicDecision(
            **claim_fields,
            workflow=baseline.workflow,
            baseline_version=baseline.version,
            candidate_version=candidate.version,
            verdict="abstain",
            recommended_action="collect_evidence",
            reason_codes=["cost_per_outcome_comparison_unavailable"],
            baseline=baseline,
            candidate=candidate,
            success_rate_change=success_change,
            policy=active_policy,
        )
    if exact_cost_change > Fraction(str(active_policy.max_cost_per_outcome_increase)):
        return EconomicDecision(
            **claim_fields,
            workflow=baseline.workflow,
            baseline_version=baseline.version,
            candidate_version=candidate.version,
            verdict="fail",
            recommended_action="investigate",
            reason_codes=["cost_per_outcome_increase_exceeds_limit"],
            baseline=baseline,
            candidate=candidate,
            success_rate_change=success_change,
            cost_per_outcome_change=cost_change,
            policy=active_policy,
        )

    return EconomicDecision(
        **claim_fields,
        workflow=baseline.workflow,
        baseline_version=baseline.version,
        candidate_version=candidate.version,
        verdict="pass",
        recommended_action="review_candidate",
        reason_codes=["economic_constraints_satisfied"],
        baseline=baseline,
        candidate=candidate,
        success_rate_change=success_change,
        cost_per_outcome_change=cost_change,
        policy=active_policy,
    )


__all__ = [
    "DecisionPolicy",
    "EvidenceFingerprint",
    "EconomicDecision",
    "RunEvidence",
    "VersionEconomics",
    "VersionEvidence",
    "compare_workflow_versions",
]
