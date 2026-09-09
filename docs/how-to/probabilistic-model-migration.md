# Evaluate a model migration under uncertainty

The public model-migration API is experimental and always returns an abstention.
Predictive reliability has not been approved; calibration readiness and a large sample
do not authorize migration. Zeroth does not alter production routing.

The example below demonstrates the paired-evidence request format with synthetic data.
It does not demonstrate a validated forecast or an approved traffic change. An unqualified
risk law returns no action distributions. More cases or simulations cannot bypass that gate.

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
        accepted=index % 100 < 97,
        critical_error=index % 100 == 0,
        source="synthetic-example:incumbent",
    )
    for index in range(600)
]
candidate = [
    MigrationObservation(
        case_id=f"case-{index}",
        cost_usd=Decimal("0.011"),
        latency_ms=1_050,
        accepted=index % 100 < 96,
        critical_error=index % 50 == 0,
        source="synthetic-example:candidate",
    )
    for index in range(600)
]
```

Do not create artificial case identities to make unrelated samples appear paired. When critical
errors are absent, Zeroth uses the approximate 95% `3/n` upper bound and may request more cases
before it evaluates a strict reliability limit.

The synthetic candidate accepts one percentage point fewer cases than the incumbent
(96% versus 97%). These constructed rates illustrate the request, not production
quality or statistical power. No sample count in this example qualifies the public
API to recommend migration.

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
        period_request_counts=[
            1_800, 2_000, 2_200, 1_900, 2_100, 2_000, 2_150, 1_950, 2_050, 1_850, 2_200, 2_000
        ],
        demand_horizon="month",
    ),
    policy=MigrationRiskPolicy(
        candidate_shares=[0.1, 0.25, 0.5, 1.0],
        max_quality_drop=0.05,
        max_p95_latency_ms=2_000,
        max_critical_error_rate=0.05,
        max_constraint_breach_probability=0.05,
        cvar_confidence=0.95,
        max_cvar_loss_usd=Decimal("1500"),
        critical_error_penalty_usd=Decimal("100"),
    ),
    calibration_observations=calibration,
    simulations=4_000,
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
and 0.01 (`critical`), split across every test in the assessment and evaluated under
the individual tests' assumptions.
Synthetic checks do not establish deployment calibration or guarantee a false-alarm rate
for arbitrary dependence, missingness or workload shift. Bias and drift additionally have to
clear the legacy materiality floors (10% of the observed mean for bias, 20% for drift; absolute
probability points for the rate metrics), so a long history cannot fail on an offset that is
statistically certain but immaterial. Each metric's p-values and standardized statistics are
returned in the decision's `evidence_lineage["forecast_readiness_tests"]`; the readiness object
itself keeps the request wire schema the SDK mirrors.

## Submit and inspect the decision

```python
client = ZerothClient(api_key="...", base_url="https://zeroth.example.com")
decision = client.create_model_migration_decision(request)

print(decision["recommended_action"])  # collect_evidence
print(decision["recommended_candidate_share"])  # 0.0
for action in decision["actions"]:
    print(
        action["candidate_share"],
        action["expected_monthly_savings_usd"],
        action["probability_quality_breach"],
        action["cvar_loss_usd"],
        action["violated_constraints"],
    )
```

The response abstains and does not recommend routing any traffic to the candidate.
Inspect `reason_codes` and `evidence_lineage` for the missing evidence and experimental
status. `forecast_status="experimental"` and `predictive_reliability="unapproved"`
remain in new retained lineage even if calibration readiness says `calibrated`.

Historical reports and private diagnostic methods can contain action-level simulated
cost, savings, latency, quality, critical-error and tail-loss summaries. Their intervals
and policy sensitivity breakpoints describe the method and supplied assumptions; they
are not validated future coverage or a reason to relax customer constraints. Original
historical values remain readable and do not acquire current-method validation.

Diagnostic `additional_cases_required`, `additional_demand_periods_required` and
`additional_simulations_required` are conditional collection estimates. They assume the
observed rates, dependence, workload and demand law remain relevant; they are not promised
sample sizes or sufficient conditions for authorization. More simulation iterations
reduce Monte Carlo uncertainty only. They do not repair an unqualified risk law, missing
source evidence, judge error or unvalidated predictive coverage.

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
forecast refresh from silently changing a previously delivered report. New PDFs use
`model-migration-v3`: simulated values and recorded actions are explicitly labeled as
experimental diagnostics, without rollout authorization. Existing v1/v2 artifact bytes
remain available unchanged. Delivery text carries the same limitation.

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

These fields describe requested diagnostic scenarios. The public response still abstains
and does not select a routing action. A private diagnostic checks critical errors per
routed cohort as well as globally; evidence in another cohort does not resolve a sparse one.

## Verify a rollout and recalibrate

The rollout API is retained for legacy recommended decisions and existing experiments.
Creation requires a retained `recommend` verdict, so a new public experimental abstention
(including `fresh` above) cannot start a rollout. The following shows the legacy interface;
`legacy_decision_id` must identify an existing recommendation in the same tenant.
The `client` below must use a self-hosted legacy JWT. Project API keys and paid
browser sessions cannot create, assign or verify a rollout, including one retained
from an older release. They can still read decision history and stop a rollout.

```python
from zeroth.protocol import RandomizedRolloutRequest, RandomizedRolloutVerifyRequest

rollout = client.create_randomized_rollout(
    RandomizedRolloutRequest(
        decision_id=legacy_decision_id,
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

- New managed backtests retain paired numeric scoring evidence. That is not the complete
  per-case cost/latency/demand artifact required for forecast harvesting; without that artifact,
  harvesting abstains. Historical aggregate-only reports remain unqualified.
- Randomized verification assumes stable subjects, assignment before execution, one comparable
  measured outcome per subject, and no concurrent treatment changes. It does not repair interference
  between subjects or unrecorded noncompliance.
- Scheduled refresh is implemented; outbound calibration-drift notifications are not.
- PDF generation and explicit email delivery are implemented. Automatic delivery after a scheduled
  refresh and material-change notification rules remain separate roadmap work.
- The first decision covers model migration. Token-capacity, retry-policy, delivery-time, and broader
  agent/prompt optimization remain separate roadmap work.
