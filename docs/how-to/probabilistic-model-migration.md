# Evaluate a model migration under uncertainty

Use the managed model-migration decision when you have paired incumbent and candidate results,
observed workload demand, and enough forecast history to check calibration. The result is
advisory; Zeroth does not alter production routing.

## Prepare paired evidence

Each `case_id` must identify the same task on both sides. Supply complete per-case measurements so
the simulator retains the observed relationship between cost, latency, quality, and failures.

```python
from datetime import UTC, datetime
from decimal import Decimal

from zeroth.protocol import (
    ForecastCalibrationObservation,
    MigrationEvidence,
    MigrationObservation,
    MigrationRiskPolicy,
    ProbabilisticMigrationRequest,
)
from zeroth.sdk import ZerothClient

incumbent = [
    MigrationObservation(
        case_id=f"case-{index}",
        cost_usd=Decimal("0.018"),
        latency_ms=1_400,
        accepted=index < 97,
        critical_error=index == 0,
        source="production",
    )
    for index in range(100)
]
candidate = [
    MigrationObservation(
        case_id=f"case-{index}",
        cost_usd=Decimal("0.011"),
        latency_ms=1_050,
        accepted=index < 96,
        critical_error=index in {0, 50},
        source="shadow",
    )
    for index in range(100)
]
```

Do not create artificial case identities to make unrelated samples appear paired. When critical
errors are absent, Zeroth uses the approximate 95% `3/n` upper bound and may request more cases
before it evaluates a strict reliability limit.

## Supply calibration history and risk limits

Calibration observations compare earlier forecast intervals with realized values. Cloud derives
readiness from these rows and ignores any readiness claim embedded in the evidence object.

```python
calibration_targets = {
    "monthly_cost_usd": (1_800, 1_650, 1_950, 1_810),
    "success_rate": (0.97, 0.95, 0.99, 0.968),
    "p95_latency_ms": (1_500, 1_300, 1_700, 1_520),
    "critical_error_rate": (0.01, 0.00, 0.03, 0.012),
}
calibration = [
    ForecastCalibrationObservation(
        forecast_id=f"{metric}-{month}",
        metric=metric,
        predicted_mean=mean,
        predicted_low=low,
        predicted_high=high,
        observed=observed,
        observed_at=datetime(2026, month, 1, tzinfo=UTC),
    )
    for metric, (mean, low, high, observed) in calibration_targets.items()
    for month in range(1, 7)
]

request = ProbabilisticMigrationRequest(
    evidence=MigrationEvidence(
        workload="support-triage",
        incumbent_model="model-a",
        candidate_model="model-b",
        incumbent=incumbent,
        candidate=candidate,
        period_request_counts=[90_000, 100_000, 110_000],
    ),
    policy=MigrationRiskPolicy(
        candidate_shares=[0.1, 0.25, 0.5, 1.0],
        max_quality_drop=0.01,
        max_p95_latency_ms=2_000,
        max_critical_error_rate=0.05,
        max_constraint_breach_probability=0.05,
        cvar_confidence=0.95,
        max_cvar_loss_usd=Decimal("1500"),
        critical_error_penalty_usd=Decimal("100"),
    ),
    calibration_observations=calibration,
    simulations=10_000,
    seed=17,
)
```

Risk limits belong to the customer. `max_constraint_breach_probability=0.05` means an action may
breach each declared quality, latency, or reliability limit in at most 5% of simulated futures.
`max_cvar_loss_usd` limits the average loss among the worst tail selected by `cvar_confidence`.
Cloud requires calibration history for cost, success rate, p95 latency, and critical-error rate;
the weakest or missing metric controls overall readiness.

Readiness is a statistical test, not a point comparison. For every metric with at least six
periods Cloud runs three tests: an exact one-sided binomial test of the covered count against the
nominal 90% level, a Student-t test of the mean residual, and a Welch t-test of the recent half of
the residuals against the earlier half. The family-wise false-alarm budgets are 0.05 (`warning`)
and 0.01 (`critical`), split across every test in the assessment, so a perfectly calibrated
forecaster is marked `calibrated` in at least 95% of assessments however many metrics or periods
it has (measured 0.97–1.00 over 3,000 synthetic replications at 6 and 12 periods), while a
forecaster whose mean is off by 25% of the true value, or whose intervals are a tenth of the true
width, is flagged in effectively every twelve-period history. Bias and drift additionally have to
clear the legacy materiality floors (10% of the observed mean for bias, 20% for drift; absolute
probability points for the rate metrics), so a long history cannot fail on an offset that is
statistically certain but immaterial. Each metric's p-values and standardized statistics are
returned in `forecast_readiness.metrics`.

## Submit and inspect the decision

```python
client = ZerothClient(api_key="...", base_url="https://zeroth.example.com")
decision = client.create_model_migration_decision(request)

print(decision["recommended_action"])
print(decision["recommended_candidate_share"])
for action in decision["actions"]:
    print(
        action["candidate_share"],
        action["expected_monthly_savings_usd"],
        action["probability_quality_breach"],
        action["cvar_loss_usd"],
        action["violated_constraints"],
    )
```

Every action includes expected cost and savings, 5th–95th percentile intervals, breach
probabilities, VaR/CVaR, feasibility reasons, and the minimum policy limits that would make the
action admissible under the same scenarios. The latter values are policy sensitivity breakpoints,
not evidence that the customer should relax its limits.

An abstention returns no action forecasts. Inspect `reason_codes` and
`additional_cases_required`; rerunning more Monte Carlo iterations cannot fix insufficient or
uncalibrated real evidence.

List retained decisions with:

```python
history = client.list_model_migration_decisions(workload="support-triage")
```

The retained record contains numeric observations, provenance, calibration status, policy, seed,
and report. It does not contain prompts or raw model output.

## Generate and deliver an immutable PDF

Create the customer-facing PDF from the retained decision, download it through the authenticated
API, or send the exact same artifact by email:

```python
from pathlib import Path

from zeroth.protocol import DecisionReportDeliveryRequest

report = client.create_decision_report(decision["decision_id"])
Path("model-migration-decision.pdf").write_bytes(
    client.download_decision_report(report["report_id"])
)
delivery = client.deliver_decision_report(
    report["report_id"],
    DecisionReportDeliveryRequest(
        recipients=["ai-platform-owner@example.com"],
        delivery_mode="attachment",
    ),
)
```

Report creation is idempotent per tenant, decision, and template version. The API response, stored
PDF, download `ETag`, and delivery audit row share the same SHA-256 digest. This prevents a later
forecast refresh from silently changing a previously delivered report.

Email delivery is disabled by default. A hosted deployment enables it with
`ECP_REPORT_EMAIL_ENABLED=true`, `ECP_REPORT_EMAIL_FROM`, and `ECP_REPORT_SMTP_HOST`; port,
credentials, STARTTLS, and timeout use the corresponding `ECP_REPORT_SMTP_*` settings. SMTP
credentials belong in deployment secrets, never API payloads or tenant connector JSON.

Use `delivery_mode="link"` to email the authenticated download URL instead of an attachment and
set `ECP_REPORT_PUBLIC_BASE_URL` to the service's external origin. The link still requires normal
Zeroth authentication; it is not a public bearer URL.

## Harvest current evidence and schedule refreshes

Cloud can rebuild evidence from tenant-scoped production and synthetic-control instrumentation.
Pairing uses the stable `subject_id`; `dimensions["cohort"]` supplies the cohort, and measured
outcomes supply acceptance and critical-error labels. A retained hosted backtest is eligible only
when its report contains explicit incumbent and candidate observations plus observed period request
counts. Aggregate success rates are never expanded into invented samples.

```python
from zeroth.protocol import (
    MigrationEvidenceRefreshRequest,
    MigrationEvidenceSource,
    ProbabilisticDecisionScheduleRequest,
)

source = MigrationEvidenceSource(
    workload="support-triage",
    incumbent_model="model-a",
    candidate_model="model-b",
    lookback_days=30,
)

fresh = client.refresh_model_migration_decision(
    MigrationEvidenceRefreshRequest(evidence_source=source, policy=request.policy)
)
schedule = client.create_probabilistic_decision_schedule(
    ProbabilisticDecisionScheduleRequest(
        evidence_source=source,
        policy=request.policy,
        interval_minutes=1440,
    )
)
```

Schedules retain the selector and policy, not an evidence snapshot. Each due run re-queries
telemetry and retained calibration observations, stores a new immutable decision, and returns an
explicit `collect_evidence` abstention when case-level evidence is unavailable.

## Optimize cohort routes

Use explicit routing actions when a candidate is appropriate for only part of the workload.

```python
from zeroth.protocol import CohortRoutingAction

cohort_policy = request.policy.model_copy(
    update={
        "routing_actions": [
            CohortRoutingAction(
                action_id="enterprise-only",
                cohort_candidate_shares={"enterprise": 1.0, "self-serve": 0.0},
            ),
            CohortRoutingAction(
                action_id="all-traffic",
                cohort_candidate_shares={"enterprise": 1.0, "self-serve": 1.0},
            ),
        ]
    }
)
```

The response identifies the selected `action_id`, effective candidate share, and exact cohort map.
Zero observed critical errors are checked per routed cohort as well as globally; a sparse cohort
causes abstention rather than inheriting confidence from a larger unrelated cohort.

## Verify a rollout and recalibrate

Create a bounded randomized experiment from a retained recommendation, ask Cloud for the sticky
assignment before execution, and then verify after measured outcomes arrive.

```python
from zeroth.protocol import RandomizedRolloutRequest, RandomizedRolloutVerifyRequest

rollout = client.create_randomized_rollout(
    RandomizedRolloutRequest(
        decision_id=fresh["decision_id"],
        candidate_probability=0.5,
        minimum_per_arm=100,
    )
)
assignment = client.assign_randomized_rollout(
    rollout["rollout_id"], subject_id="account-42", cohort="enterprise"
)
# Route account-42 to assignment["assigned_model"], then emit normal execution/outcome events.
verification = client.verify_randomized_rollout(
    rollout["rollout_id"],
    RandomizedRolloutVerifyRequest(bootstrap_samples=2_000, seed=17),
)
```

Verification uses only post-assignment observations, reports arm sizes and bootstrap intervals, and
is `inconclusive` below the declared sample floor. Any observed model-assignment noncompliance makes
the causal result `invalid`; Zeroth does not silently delete contaminated subjects and keep a causal
label. A verified run appends cost, success, p95 latency, and critical-error forecast-versus-observed
rows. Subsequent scheduled decisions use those rows for calibration and drift checks.

## Current limitations

- The managed backtest executor currently retains aggregate scores unless its executor supplies an
  explicit case-level economic artifact. Aggregate-only historical backtests correctly abstain.
- Randomized verification assumes stable subjects, assignment before execution, one comparable
  measured outcome per subject, and no concurrent treatment changes. It does not repair interference
  between subjects or unrecorded noncompliance.
- Scheduled refresh is implemented; outbound calibration-drift notifications are not.
- PDF generation and explicit email delivery are implemented. Automatic delivery after a scheduled
  refresh and material-change notification rules remain separate roadmap work.
- The first decision covers model migration. Token-capacity, retry-policy, delivery-time, and broader
  agent/prompt optimization remain separate roadmap work.
