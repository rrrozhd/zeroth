"""Shared fixtures for the forecast-engine validity tests (private diagnostic path)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from zeroth.econ import probabilistic as subject

QUALIFICATION_TIME = datetime(2026, 9, 3, tzinfo=UTC)


def observations(prefix, *, count, cost, accepted_until, critical_every=None, cohort="default"):
    return [
        subject.MigrationObservation(
            case_id=f"case-{index}",
            cohort=cohort,
            cost_usd=Decimal(cost),
            latency_ms=800 + (index % 5) * 10,
            accepted=index < accepted_until,
            critical_error=(critical_every is not None and index % critical_every == 0),
            source=prefix,
        )
        for index in range(count)
    ]


def evidence(
    *,
    count=600,
    periods=12,
    demand=1_000,
    incumbent_accepted=None,
    candidate_accepted=None,
    candidate_critical_every=None,
    candidate_cost="0.50",
):
    return subject.MigrationEvidence(
        workload="invoice-agent",
        incumbent_model="model-a",
        candidate_model="model-b",
        incumbent=observations(
            "production",
            count=count,
            cost="1.00",
            accepted_until=count if incumbent_accepted is None else incumbent_accepted,
        ),
        candidate=observations(
            "replay",
            count=count,
            cost=candidate_cost,
            accepted_until=count if candidate_accepted is None else candidate_accepted,
            critical_every=candidate_critical_every,
        ),
        period_request_counts=[demand] * periods,
        demand_horizon="month",
        readiness=subject.ForecastReadiness(calibration_state="calibrated", drift_state="stable"),
    )


def policy(**changes):
    values = dict(
        min_paired_cases=30,
        candidate_shares=[1.0],
        max_quality_drop=0.05,
        max_p95_latency_ms=10_000,
        max_critical_error_rate=1.0,
        max_constraint_breach_probability=0.05,
        max_cvar_loss_usd=Decimal("0"),
    )
    values.update(changes)
    return subject.MigrationRiskPolicy(**values)


def diagnose(world, rules, *, simulations=2_000, seed=7):
    action_ids = (
        tuple(action.action_id for action in rules.routing_actions)
        if rules.routing_actions
        else tuple(f"global-{share:g}" for share in rules.candidate_shares)
    )
    qualification = subject._ExperimentalRiskQualification(
        qualification_id="validity-test-qualification",
        workload=world.workload,
        incumbent_model=world.incumbent_model,
        candidate_model=world.candidate_model,
        action_ids=action_ids,
        metric_supports=(
            ("monthly_cost_usd", (0.0, 1_000_000.0)),
            ("p95_latency_ms", (0.0, 1_000_000.0)),
            ("success_rate", (0.0, 1.0)),
            ("critical_error_rate", (0.0, 1.0)),
        ),
        loss_support=(-1_000_000.0, 1_000_000.0),
        currency="USD",
        loss_formula_version="incremental-cost-critical-penalty-v1",
        independent_unit="paired-request-and-independent-month",
        dependence_kind="independent_paired_requests_and_periods",
        artifact_sha256="d" * 64,
        issuer="validity-tests",
        valid_from=QUALIFICATION_TIME,
        valid_until=QUALIFICATION_TIME,
        active=True,
    )
    return subject._diagnose_model_migration(
        world,
        policy=rules,
        simulations=simulations,
        seed=seed,
        _qualification=qualification,
        _qualification_checked_at=QUALIFICATION_TIME,
    )
