"""Evidence-gated economic change decisions for versioned AI workflows.

The module is deliberately independent of persistence and transport. Hosted and
local callers normalize their execution/outcome evidence into ``RunEvidence``
and receive the same pass/fail/abstain decision. A missing or estimated dollar
never becomes a confident approval by default.

Every economic constraint is judged on a two-sided confidence interval at the
policy's ``confidence_level``, never on a point estimate. ``fail`` means the
interval lies entirely beyond a limit; ``pass`` means it lies entirely within
every limit; anything else is ``abstain`` with an estimate of how many more runs
per version would make the undetermined gates decisive. Before this rule, with
ten runs per version, two identical versions were failed half the time and a
candidate ten points worse was approved a quarter of the time.
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import datetime
from decimal import Decimal
from fractions import Fraction
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from zeroth.econ.measurement import MeasurementState
from zeroth.econ.plane.statistics.intervals import (
    log_ratio_of_sums_variance,
    newcombe_difference_interval,
    required_sample_size,
    t_quantile,
    wilson_interval,
)

#: Reason codes for gates whose interval straddles the limit. They accompany an
#: ``abstain`` verdict together with ``additional_runs_required``.
SUCCESS_RATE_MINIMUM_UNDETERMINED = "candidate_success_rate_minimum_undetermined"
SUCCESS_RATE_DROP_UNDETERMINED = "success_rate_drop_undetermined"
COST_PER_OUTCOME_CHANGE_UNDETERMINED = "cost_per_outcome_change_undetermined"



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

    version: Literal[
        "stored-assertions/1", "stored-assertions/2", "stored-assertions/3", "stored-assertions/4",
        "stored-assertions/5",
    ] = "stored-assertions/1"
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    cost_revision_records: int = Field(default=0, ge=0)
    execution_records: int = Field(ge=0)
    outcome_records: int = Field(ge=0)


class SourceDelivery(BaseModel):
    """Reconciliation with caller inventory; no guarantee of physical source truth."""

    model_config = ConfigDict(extra="forbid")

    source_window_id: str
    inventory_version: Literal["source-inventory/1"] = "source-inventory/1"
    inventory_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["matched", "mismatch"]
    expected_runs: int = Field(ge=0)
    observed_runs: int = Field(ge=0)
    expected_executions: int = Field(ge=0)
    observed_executions: int = Field(ge=0)
    missing_runs: int = Field(ge=0)
    unexpected_runs: int = Field(ge=0)
    mismatched_runs: int = Field(ge=0)
    out_of_window_executions: int = Field(ge=0)
    scan_truncated: bool = False


class ChargeOwnership(BaseModel):
    """Declared monetary owners; no inference of provider billing truth."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["declared", "unverified"]
    owned_charge_records: int = Field(ge=0)
    summary_records: int = Field(ge=0)
    unattributed_records: int = Field(ge=0)


class OutcomeSemantics(BaseModel):
    """The selected immutable success rule; no claim of business label maturity."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["defined", "missing", "type_mismatch"]
    definition_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    rule_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _binding_matches_status(self) -> OutcomeSemantics:
        if self.status == "missing":
            if self.definition_digest is not None or self.rule_digest is not None:
                raise ValueError("missing outcome semantics cannot carry a definition binding")
        elif self.definition_digest is None or self.rule_digest is None:
            raise ValueError("present outcome semantics require both definition and rule digests")
        return self


class VersionEvidence(BaseModel):
    """All in-window run evidence for one exact workflow version."""

    model_config = ConfigDict(extra="forbid")

    workflow: str = Field(min_length=1)
    version: str = Field(min_length=1)
    runs: list[RunEvidence] = Field(default_factory=list)
    source_fingerprint: EvidenceFingerprint | None = None
    source_delivery: SourceDelivery | None = None
    charge_ownership: ChargeOwnership | None = None
    outcome_semantics: OutcomeSemantics | None = None


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
    confidence_level: float = Field(default=0.95, gt=0, lt=1)


class ConfidenceInterval(BaseModel):
    """Two-sided interval on a comparison statistic at the policy's confidence level."""

    model_config = ConfigDict(extra="forbid")

    low: float
    high: float
    confidence_level: float
    method: str


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


class CalculationInput(BaseModel):
    """One distinct normalized input tuple and its run multiplicity; no source IDs."""

    model_config = ConfigDict(extra="forbid")

    cost_usd: Decimal | None = Field(default=None, ge=0)
    cost_measurement: MeasurementState
    accepted: bool | None
    outcome_measurement: MeasurementState
    runs: int = Field(ge=1, strict=True)

    @model_validator(mode="after")
    def _cost_matches_provenance(self) -> CalculationInput:
        if (self.cost_measurement is MeasurementState.UNMEASURED) != (self.cost_usd is None):
            raise ValueError("unknown cost requires unmeasured provenance and no amount")
        return self


class CalculationInputs(BaseModel):
    """Portable arithmetic inputs, not a source snapshot or completeness proof."""

    model_config = ConfigDict(extra="forbid")

    version: Literal["run-economics/1"] = "run-economics/1"
    baseline: list[CalculationInput]
    candidate: list[CalculationInput]


def _calculation_rows(runs: list[RunEvidence]) -> list[CalculationInput]:
    grouped = Counter(
        (run.cost_usd, run.cost_measurement, run.accepted, run.outcome_measurement)
        for run in runs
    )
    rows = []
    for (cost, state, accepted, provenance), count in grouped.items():
        # Equal decimals serialize equally without Decimal.normalize()'s context rounding.
        if cost is not None:
            amount = format(cost, "f")
            canonical = amount.rstrip("0").rstrip(".") if "." in amount else amount
            cost = Decimal(canonical) if cost else Decimal(0)
        rows.append(CalculationInput(
            cost_usd=cost, cost_measurement=state, accepted=accepted,
            outcome_measurement=provenance, runs=count,
        ))
    return sorted(rows, key=lambda row: row.model_dump_json())


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
    source_delivery: dict[Literal["baseline", "candidate"], SourceDelivery] = Field(
        default_factory=dict
    )
    charge_ownership: dict[Literal["baseline", "candidate"], ChargeOwnership] = Field(
        default_factory=dict
    )
    outcome_semantics: dict[Literal["baseline", "candidate"], OutcomeSemantics] = Field(
        default_factory=dict
    )
    calculation_inputs: CalculationInputs | None = None
    #: Interval on ``success_rate_change`` (Newcombe hybrid score).
    success_rate_change_interval: ConfidenceInterval | None = None
    #: Interval on ``cost_per_outcome_change`` (delta method on the log ratio).
    cost_per_outcome_change_interval: ConfidenceInterval | None = None
    #: Wilson interval on the candidate's own success rate.
    candidate_success_rate_interval: ConfidenceInterval | None = None
    #: Further runs per version, beyond the smaller of the two, estimated to make
    #: every undetermined gate decisive if the observed rates and dispersions hold.
    additional_runs_required: int | None = None




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
def _smoothed_binomial_variance(successes: int, n: int) -> float:
    """Per-observation variance of a proportion with a Wilson-style pseudo-count.

    The pseudo-count keeps a sample with no failures (or no successes) from
    reporting zero variance, which would make the required sample size zero.
    """
    if n <= 0:
        return 0.25
    p = (successes + 1.0) / (n + 2.0)
    return p * (1.0 - p)


def _cost_pairs(evidence: VersionEvidence) -> tuple[list[float], list[float]]:
    """Per-run ``(cost, accepted)`` pairs for the comparable-cost ratio."""
    # Normalize exact amounts before floating-point variance calculations so
    # changing the monetary unit cannot change the interval or verdict.
    exact = [Fraction(run.cost_usd or Decimal("0")) for run in evidence.runs]
    scale = max(exact, default=Fraction(0)) or Fraction(1)
    costs = [float(value / scale) for value in exact]
    accepted = [1.0 if run.accepted is True else 0.0 for run in evidence.runs]
    return costs, accepted


def _cost_change_interval(
    baseline_evidence: VersionEvidence,
    candidate_evidence: VersionEvidence,
    confidence: float,
    exact_cost_change: Fraction,
) -> tuple[ConfidenceInterval | None, float]:
    """Interval on the relative change in cost per accepted outcome.

    Returns the interval (``None`` when a version has fewer than two runs) and the
    per-observation variance used for the required-sample-size estimate. A
    candidate with zero comparable cost is an exact 100% decrease.
    """
    base_costs, base_accepted = _cost_pairs(baseline_evidence)
    cand_costs, cand_accepted = _cost_pairs(candidate_evidence)
    if len(base_costs) < 2 or len(cand_costs) < 2:
        return None, 0.0
    if sum(cand_costs) == 0.0:
        return ConfidenceInterval(
            low=-1.0, high=-1.0, confidence_level=confidence, method="exact_zero_cost"
        ), 0.0
    base_variance = log_ratio_of_sums_variance(base_costs, base_accepted)
    cand_variance = log_ratio_of_sums_variance(cand_costs, cand_accepted)
    degrees = len(base_costs) + len(cand_costs) - 2
    half_width = t_quantile(confidence, degrees) * math.sqrt(base_variance + cand_variance)
    change = float(exact_cost_change)
    if half_width == 0:
        low = high = change
    else:
        # Add one while the change is still exact: converting a near-total
        # saving first can round a positive cost ratio to zero.
        center = math.log(float(1 + exact_cost_change))
        low = math.expm1(center - half_width)
        high = math.expm1(center + half_width)
    per_observation = base_variance * len(base_costs) + cand_variance * len(cand_costs)
    return (
        ConfidenceInterval(
            low=low,
            high=high,
            confidence_level=confidence,
            method="delta_method_log_ratio_t",
        ),
        per_observation,
    )



def compare_workflow_versions(
    baseline_evidence: VersionEvidence,
    candidate_evidence: VersionEvidence,
    *,
    policy: DecisionPolicy | None = None,
) -> EconomicDecision:
    """Compare a candidate to a baseline without manufacturing confidence.

    ``abstain`` means the evidence contract was not met, or that the evidence
    met the contract but an interval still straddles a limit; the reason codes
    say which, and ``additional_runs_required`` estimates the shortfall. ``fail``
    means an interval establishes a breach of an economic or outcome constraint.
    ``pass`` means every interval lies within its limit; it does not claim a
    causal effect beyond the supplied evidence window.
    """
    if baseline_evidence.workflow != candidate_evidence.workflow:
        raise ValueError("baseline and candidate must describe the same workflow")
    active_policy = policy or DecisionPolicy()
    semantics = {
        label: evidence.outcome_semantics
        for label, evidence in (("baseline", baseline_evidence), ("candidate", candidate_evidence))
        if evidence.outcome_semantics is not None
    }
    claim_fields = {
        "calculation_inputs": CalculationInputs(
            baseline=_calculation_rows(baseline_evidence.runs),
            candidate=_calculation_rows(candidate_evidence.runs),
        ),
        "claim_class": "observed_comparison",
        "method_version": "interval-policy/1",
        "limitations": [
            "source_completeness_unverified",
            "no_statistical_causal_or_forecast_authorization",
            "outcome_maturity_unverified" if semantics else "outcome_semantics_unverified",
        ],
        "outcome_semantics": semantics,
        "source_evidence": {
            label: evidence.source_fingerprint
            for label, evidence in (
                ("baseline", baseline_evidence), ("candidate", candidate_evidence)
            )
            if evidence.source_fingerprint is not None
        },
        "source_delivery": {
            label: evidence.source_delivery
            for label, evidence in (
                ("baseline", baseline_evidence), ("candidate", candidate_evidence)
            )
            if evidence.source_delivery is not None
        },
        "charge_ownership": {
            label: evidence.charge_ownership
            for label, evidence in (
                ("baseline", baseline_evidence), ("candidate", candidate_evidence)
            )
            if evidence.charge_ownership is not None
        },
    }
    confidence = active_policy.confidence_level

    baseline = _summarize(
        baseline_evidence, allow_estimated_cost=active_policy.allow_estimated_cost
    )
    candidate = _summarize(
        candidate_evidence, allow_estimated_cost=active_policy.allow_estimated_cost
    )

    def decision(**fields: object) -> EconomicDecision:
        return EconomicDecision(
            **claim_fields,
            workflow=baseline.workflow,
            baseline_version=baseline.version,
            candidate_version=candidate.version,
            baseline=baseline,
            candidate=candidate,
            policy=active_policy,
            **fields,  # type: ignore[arg-type]
        )

    evidence_reasons = [
        *_evidence_reasons("baseline", baseline, active_policy),
        *_evidence_reasons("candidate", candidate, active_policy),
    ]
    for label, evidence in (("baseline", baseline_evidence), ("candidate", candidate_evidence)):
        if evidence.source_delivery is not None and evidence.source_delivery.status != "matched":
            evidence_reasons.append(f"{label}_source_delivery_mismatch")
        if semantics and (
            evidence.outcome_semantics is None or evidence.outcome_semantics.status != "defined"
        ):
            evidence_reasons.append(f"{label}_outcome_definition_unavailable")
    if (
        len(semantics) == 2
        and all(value.status == "defined" for value in semantics.values())
        and semantics["baseline"].rule_digest != semantics["candidate"].rule_digest
    ):
        evidence_reasons.append("outcome_semantics_incompatible")
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
        return decision(

            verdict="abstain",
            recommended_action="collect_evidence",
            reason_codes=evidence_reasons,
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

    _diff, drop_low, drop_high = newcombe_difference_interval(
        baseline.accepted_runs,
        baseline.labeled_runs,
        candidate.accepted_runs,
        candidate.labeled_runs,
        confidence,
    )
    drop_interval = ConfidenceInterval(
        low=drop_low,
        high=drop_high,
        confidence_level=confidence,
        method="newcombe_hybrid_score",
    )
    rate_hat, rate_low, rate_high = wilson_interval(
        candidate.accepted_runs, candidate.labeled_runs, confidence
    )
    rate_interval = ConfidenceInterval(
        low=rate_low,
        high=rate_high,
        confidence_level=confidence,
        method="wilson_score",
    )
    intervals = {
        "success_rate_change_interval": drop_interval,
        "candidate_success_rate_interval": rate_interval,
    }

    undetermined: list[str] = []
    required: list[int] = []
    outcome_failures: list[str] = []
    if candidate_success is None or candidate.accepted_runs == 0:
        outcome_failures.append("candidate_has_no_accepted_outcomes")
    else:
        minimum = active_policy.min_success_rate
        if rate_high < minimum:
            outcome_failures.append("candidate_success_rate_below_minimum")
        elif rate_low < minimum:
            undetermined.append(SUCCESS_RATE_MINIMUM_UNDETERMINED)
            required.append(
                required_sample_size(
                    _smoothed_binomial_variance(candidate.accepted_runs, candidate.labeled_runs),
                    abs(rate_hat - minimum),
                    confidence,
                )
                - candidate.labeled_runs
            )
        limit = -active_policy.max_success_rate_drop
        if drop_high < limit:

            outcome_failures.append("candidate_success_rate_drop_exceeds_limit")
        elif drop_low < limit:
            undetermined.append(SUCCESS_RATE_DROP_UNDETERMINED)
            variance = _smoothed_binomial_variance(
                baseline.accepted_runs, baseline.labeled_runs
            ) + _smoothed_binomial_variance(candidate.accepted_runs, candidate.labeled_runs)
            required.append(
                required_sample_size(variance, abs(_diff - limit), confidence)
                - min(baseline.labeled_runs, candidate.labeled_runs)
            )
    if outcome_failures:
        return decision(

            verdict="fail",
            recommended_action="hold",
            reason_codes=outcome_failures,
            success_rate_change=success_change,
            cost_per_outcome_change=cost_change,
            **intervals,
        )

    if cost_change is None:
        return decision(

            verdict="abstain",
            recommended_action="collect_evidence",
            reason_codes=["cost_per_outcome_comparison_unavailable"],
            success_rate_change=success_change,
            **intervals,
        )

    cost_interval, cost_variance = _cost_change_interval(
        baseline_evidence, candidate_evidence, confidence, exact_cost_change
    )
    intervals["cost_per_outcome_change_interval"] = cost_interval
    cost_limit = active_policy.max_cost_per_outcome_increase
    if cost_interval is not None and cost_interval.low > cost_limit:
        return decision(

            verdict="fail",
            recommended_action="investigate",
            reason_codes=["cost_per_outcome_increase_exceeds_limit"],
            success_rate_change=success_change,
            cost_per_outcome_change=cost_change,
            **intervals,
        )
    if cost_interval is None or cost_interval.high > cost_limit:
        undetermined.append(COST_PER_OUTCOME_CHANGE_UNDETERMINED)
        distance = (
            abs(math.log1p(cost_change) - math.log1p(cost_limit)) if cost_change > -1 else 1.0
        )
        required.append(
            required_sample_size(cost_variance, distance, confidence)
            - min(baseline.runs, candidate.runs)
        )

    if undetermined:
        return decision(
            verdict="abstain",
            recommended_action="collect_evidence",
            reason_codes=undetermined,
            success_rate_change=success_change,
            cost_per_outcome_change=cost_change,
            additional_runs_required=max(0, max(required)),
            **intervals,
        )

    return decision(

        verdict="pass",
        recommended_action="review_candidate",
        reason_codes=["economic_constraints_satisfied"],
        success_rate_change=success_change,
        cost_per_outcome_change=cost_change,
        **intervals,
    )


__all__ = [
    "COST_PER_OUTCOME_CHANGE_UNDETERMINED",
    "SUCCESS_RATE_DROP_UNDETERMINED",
    "SUCCESS_RATE_MINIMUM_UNDETERMINED",
    "ConfidenceInterval",
    "DecisionPolicy",
    "EvidenceFingerprint",
    "EconomicDecision",
    "RunEvidence",
    "VersionEconomics",
    "VersionEvidence",
    "compare_workflow_versions",
]
