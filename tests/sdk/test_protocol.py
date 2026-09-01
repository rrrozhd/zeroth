"""Public wire contracts for the lean SaaS SDK."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

def test_execution_and_outcome_events_are_json_safe() -> None:
    from zeroth.protocol import ExecutionEvent, OutcomeEvent

    execution = ExecutionEvent(
        workflow="invoice-processing",
        workflow_version="v7",
        run_id="run-1",
        step="extract",
        attempt=2,
        cost_usd=Decimal("0.031"),
        latency_ms=420,
        recorded_at=datetime(2026, 8, 31, tzinfo=UTC),
        subject_id="customer-4",
        dimensions={"plan": "pro"},
    )
    outcome = OutcomeEvent(
        workflow="invoice-processing",
        workflow_version="v7",
        run_id="run-1",
        accepted=True,
        value_usd=Decimal("1.20"),
        score=0.99,
        occurred_at=datetime(2026, 8, 31, tzinfo=UTC),
    )

    assert execution.model_dump(mode="json")["cost_usd"] == "0.031"
    assert execution.model_dump(mode="json")["workflow_version"] == "v7"
    assert execution.model_dump(mode="json")["attempt"] == 2
    assert outcome.model_dump(mode="json")["value_usd"] == "1.20"


def test_unmeasured_execution_cannot_manufacture_a_zero_cost() -> None:
    from zeroth.protocol import ExecutionEvent

    with pytest.raises(ValidationError, match="unmeasured cost must not include a value"):
        ExecutionEvent(
            workflow="invoice-processing",
            run_id="run-1",
            step="extract",
            cost_usd=Decimal("0"),
            cost_measurement="unmeasured",
        )


def test_backtest_constraints_reject_invalid_rates() -> None:
    from zeroth.protocol import EconomicConstraints

    with pytest.raises(ValidationError):
        EconomicConstraints(min_success_rate=1.1)


def test_backtest_request_carries_candidate_and_governance_constraints() -> None:
    from zeroth.protocol import BacktestRequest, EconomicConstraints

    request = BacktestRequest(
        workflow="invoice-processing",
        candidate={"model": "gpt-5-mini", "max_retries": 1},
        constraints=EconomicConstraints(
            min_success_rate=0.97,
            max_cost_per_outcome_usd=Decimal("0.20"),
        ),
    )

    assert request.candidate["model"] == "gpt-5-mini"
    assert request.constraints.min_success_rate == 0.97


def test_backtest_request_carries_bounded_ephemeral_cases() -> None:
    import zeroth.protocol as protocol

    backtest_case = getattr(protocol, "BacktestCase", None)
    assert backtest_case is not None, "the SDK must expose bounded backtest cases"

    request = protocol.BacktestRequest(
        workflow="invoice-processing",
        baseline_version="v7",
        node_id="extract",
        incumbent_model="openai/gpt-5-mini",
        instruction="Extract the invoice fields.",
        candidate={"model": "openai/gpt-5-nano"},
        cases=[
            backtest_case(
                id="invoice-1",
                input={"text": "Invoice 1 total 12.50"},
                expected={"total": "12.50"},
            )
        ],
        constraints=protocol.EconomicConstraints(min_success_rate=0.95),
    )

    assert request.baseline_version == "v7"
    assert request.node_id == "extract"
    assert request.cases[0].id == "invoice-1"
    assert request.cases[0].input == {"text": "Invoice 1 total 12.50"}


def test_version_comparison_request_carries_evidence_policy() -> None:
    from zeroth.protocol import DecisionPolicy, VersionComparisonRequest

    request = VersionComparisonRequest(
        workflow="invoice-processing",
        baseline_version="v6",
        candidate_version="v7",
        policy=DecisionPolicy(
            min_runs=20,
            min_outcome_coverage=0.9,
            max_cost_per_outcome_increase=0.05,
        ),
    )

    assert request.policy.min_runs == 20
    assert request.policy.min_outcome_coverage == 0.9


def test_decision_schedule_request_bounds_frequency() -> None:
    from zeroth.protocol import DecisionScheduleRequest

    request = DecisionScheduleRequest(
        workflow="invoice-processing",
        baseline_version="v6",
        candidate_version="v7",
        interval_minutes=1440,
    )
    assert request.interval_minutes == 1440

    with pytest.raises(ValidationError):
        DecisionScheduleRequest(
            workflow="invoice-processing",
            baseline_version="v6",
            candidate_version="v7",
            interval_minutes=30,
        )
