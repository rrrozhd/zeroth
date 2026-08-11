"""Regression tests for econ_plane service query + costing defects (audit B5/B6/B7)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from zeroth.econ.plane.capabilities.models import Capability, Experiment
from zeroth.econ.plane.capabilities.service import active_experiment
from zeroth.econ.plane.counterfactual.models import ValueEstimate
from zeroth.econ.plane.counterfactual.service import _pick_interval
from zeroth.econ.plane.database import Base
from zeroth.econ.plane.instrumentation.models import OutcomeEvent
from zeroth.econ.plane.performance.service import calculate_snapshots

_NOW = datetime(2026, 7, 1, tzinfo=UTC)


@pytest.fixture
def econ_session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        yield session


def _value_estimate(cap_id: str, value: float) -> ValueEstimate:
    return ValueEstimate(
        tenant_id="default",
        valuation_run_id=1,
        capability_id=cap_id,
        period_start=_NOW,
        period_end=_NOW,
        estimated_value_usd=value,
        estimated_cost_usd=10.0,
        net_margin_usd=value - 10.0,
        credible_interval_low_usd=value * 0.9,
        credible_interval_high_usd=value * 1.1,
    )


def _outcome(outcome_type: str, outcome_value: str) -> OutcomeEvent:
    return OutcomeEvent(
        outcome_type=outcome_type,
        outcome_value=outcome_value,
        outcome_payload_json={},
    )


# --- B5: performance snapshot query tolerates >1 estimate per capability -----


def test_calculate_snapshots_tolerates_multiple_value_estimates(econ_session) -> None:
    # A capability accrues one ValueEstimate per valuation run; two is the common
    # case by the time the dashboard opens. Before the .limit(1) fix the
    # scalar_one_or_none() raised MultipleResultsFound and 500'd every read.
    econ_session.add(Capability(id="cap1", name="Cap One"))
    econ_session.add(_value_estimate("cap1", 100.0))
    econ_session.add(_value_estimate("cap1", 250.0))  # latest (higher id)
    econ_session.commit()

    snapshots = calculate_snapshots(econ_session)  # must not raise

    cap_snaps = [s for s in snapshots if s.capability_id == "cap1"]
    assert len(cap_snaps) == 1
    # Latest estimate (id desc) drives the snapshot: value 250 / cost 10 => aer 25.
    assert cap_snaps[0].aer == pytest.approx(25.0)


# --- B7: active_experiment tolerates >1 ACTIVE row per (capability, mode) -----


def test_active_experiment_tolerates_multiple_active_rows(econ_session) -> None:
    # Nothing enforces a single ACTIVE experiment per (capability, mode); a second
    # ACTIVE row made scalar_one_or_none() raise MultipleResultsFound, 500'ing
    # every execution ingest.
    econ_session.add(Capability(id="cap1", name="Cap One"))
    for _ in range(2):
        econ_session.add(
            Experiment(
                capability_id="cap1",
                mode="AB",
                impl_a_id="impl-a",
                impl_b_id="impl-b",
                start_at=_NOW,
                status="ACTIVE",
            )
        )
    econ_session.commit()

    exp = active_experiment(econ_session, "cap1", "AB")  # must not raise

    assert exp is not None
    assert exp.status == "ACTIVE"


# --- B6: binary-outcome estimate is dollar-denominated, not a success count ---


def test_pick_interval_conversion_is_dollar_denominated() -> None:
    # 50/100 conversions at $120 each -> proxy dollars = $6000, NOT a count of 50.
    outcomes = [_outcome("conversion", "1") for _ in range(50)]
    outcomes += [_outcome("conversion", "0") for _ in range(50)]
    values = [120.0] * 50 + [0.0] * 50

    method, estimated, low, high = _pick_interval(values, outcomes, 0.95, "PROXY")

    assert method == "wilson_binomial"
    assert estimated == pytest.approx(sum(values))
    assert estimated == pytest.approx(6000.0)  # dollars, not the ~50 count
    assert low <= estimated <= high


def test_pick_interval_high_fraud_rate_is_net_negative() -> None:
    # 60/100 flagged: proxy = 60*(-$80) + 40*(+$40) = -$3200 (net negative). The
    # old branch returned a positive success count (~+60), a sign inversion.
    outcomes = [_outcome("fraud_flag", "1") for _ in range(60)]
    outcomes += [_outcome("fraud_flag", "0") for _ in range(40)]
    values = [-80.0] * 60 + [40.0] * 40

    _method, estimated, low, high = _pick_interval(values, outcomes, 0.95, "PROXY")

    assert estimated == pytest.approx(sum(values))
    assert estimated == pytest.approx(-3200.0)
    assert estimated < 0
    assert low <= estimated <= high
