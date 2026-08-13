"""Econ dashboard reads are bounded, and the SQL rewrite kept the old numbers.

ZER-48 / A01-49.  Every function in ``zeroth.econ.plane.dashboard.service`` used
to ``select()`` a whole table and aggregate it in Python, so memory and latency
grew with total history rather than with what the panel renders.  The rewrite
moved the arithmetic into SQL under explicit ``LIMIT``s.

Nothing in the repository imported the module, so the rewrite shipped with no
executing test at all.  Two obligations follow, and this file carries both:

* **Bounds** -- seed more rows than the constant and assert the read stops at
  it.  ``policy_timeline``, the three trends and ``action_suppression`` were
  genuinely unbounded before, so those assertions discriminate.
* **Equivalence** -- the aggregates moved from Python to SQL, and
  ``portfolio_confidence_score`` in particular went from ``sum(...)/len(...)``
  over a materialised list to ``avg(case(...))``.  The exact-value assertions
  below re-derive the pre-rewrite Python expression and compare against it, so a
  drift in the SQL clamp is caught rather than blessed.

``capital_destroyers`` is the sharpest of the lot: it used to call
``top_creators`` -- already truncated to the five *best* -- and re-sort those
five ascending, so the genuinely worst capability could never appear.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from zeroth.econ.plane.capabilities.models import Capability
from zeroth.econ.plane.costing.models import CalibrationMetric, CostEstimate, GroundTruthCost
from zeroth.econ.plane.counterfactual.models import ValueEstimate
from zeroth.econ.plane.dashboard.service import (
    COMPARISON_ROWS,
    LEADERBOARD_ROWS,
    TIMELINE_ROWS,
    TREND_POINTS,
    action_suppression,
    calibration_trend,
    capability_ranking,
    capital_destroyers,
    confidence_gate_status,
    confidence_trend,
    data_quality_mix,
    drift_timeline,
    efficiency_trend,
    estimated_vs_ground_truth_cost,
    implementation_compare,
    kpis,
    policy_timeline,
    top_creators,
)
from zeroth.econ.plane.database import Base
from zeroth.econ.plane.enforcement.models import PolicyAction
from zeroth.econ.plane.performance.models import PerformanceSnapshot
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext

_NOW = datetime(2026, 8, 12, tzinfo=UTC)

#: Tolerance for the SQL-vs-Python comparisons.  ``Numeric(14, 4)`` round-trips
#: through ``Decimal`` on SQLite and ``avg`` accumulates in float, so the last
#: bits may differ from a Python ``sum(...)/len(...)``; a semantic change moves
#: these numbers far more than this.
_REL = 1e-6


@pytest.fixture
def session():  # noqa: ANN201
    """Give each test its own empty in-memory econ schema.

    Function-scoped on purpose: the bound tests below seed several hundred rows,
    which would otherwise contaminate the exact-value assertions.
    """
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(bind=engine)
    with Session(engine) as inner:
        yield ScopedSession(inner, TenantWideScopeContext.for_default_compatibility())


def _add(session: ScopedSession, rows: list) -> None:
    """Stage every row on the scoped session, which proxies ``add`` but not ``add_all``."""
    for row in rows:
        session.add(row)


def _estimate(
    capability_id: str = "cap",
    *,
    value: float = 100.0,
    cost: float = 10.0,
    margin: float | None = None,
    low: float | None = None,
    high: float | None = None,
    confidence_level: float = 0.95,
    gate_passed: bool = False,
    drift: float = 0.0,
    period_end: datetime | None = None,
    implementation_id: str | None = None,
) -> ValueEstimate:
    """Build a ValueEstimate with every non-nullable column populated."""
    return ValueEstimate(
        tenant_id="default",
        valuation_run_id=1,
        capability_id=capability_id,
        implementation_id=implementation_id,
        period_start=_NOW,
        period_end=period_end or _NOW,
        estimated_value_usd=value,
        estimated_cost_usd=cost,
        net_margin_usd=value - cost if margin is None else margin,
        credible_interval_low_usd=value * 0.9 if low is None else low,
        credible_interval_high_usd=value * 1.1 if high is None else high,
        confidence_level=confidence_level,
        confidence_gate_passed=gate_passed,
        drift_score=drift,
    )


def _python_confidence(estimate: ValueEstimate) -> float:
    """Re-derive one row's confidence the way the pre-rewrite Python did.

    Kept as an independent expression rather than a constant so the SQL
    ``case``/``avg`` form is compared against the semantics it replaced.
    """
    width = float(estimate.credible_interval_high_usd) - float(estimate.credible_interval_low_usd)
    denominator = max(abs(float(estimate.estimated_value_usd)), 1.0)
    return max(0.0, 1 - (width / denominator))


def _cost_estimate(capability_id: str, *, total: float = 1.0, quality: str = "measured"):  # noqa: ANN202
    """Build a CostEstimate row."""
    return CostEstimate(
        tenant_id="default",
        capability_id=capability_id,
        period_start=_NOW,
        period_end=_NOW,
        total_cost_estimate_usd=total,
        data_quality=quality,
    )


def _policy_action(index: int) -> PolicyAction:
    """Build a PolicyAction row."""
    return PolicyAction(
        tenant_id="default",
        capability_id=f"cap{index}",
        proposed_at=_NOW,
        action_type="throttle",
        status="PROPOSED",
    )


# --- kpis: the SQL aggregate must return what the Python loop returned --------


def test_kpis_over_an_empty_table_is_all_zero(session: ScopedSession) -> None:
    """An empty portfolio reports zeros, not a division by an empty average."""
    assert kpis(session) == {
        "total_ai_spend_usd": 0.0,
        "total_ai_value_usd": 0.0,
        "net_ai_margin_usd": 0.0,
        "portfolio_confidence_score": 0.0,
        "efficiency_index": 0.0,
    }


def _kpi_seed() -> list[ValueEstimate]:
    """Four estimates that exercise every branch of the confidence expression.

    Row 3's interval is wider than its value, so the raw score is negative and
    must clamp to zero.  Row 4's value is below one dollar, so the denominator
    must clamp *up* to one -- read literally it would score 0.6, not 0.8.
    """
    return [
        _estimate("cap-a", value=100.0, cost=10.0, low=90.0, high=110.0),
        _estimate("cap-b", value=200.0, cost=20.0, low=180.0, high=220.0),
        _estimate("cap-c", value=50.0, cost=5.0, low=0.0, high=200.0),
        _estimate("cap-d", value=0.5, cost=0.5, low=0.4, high=0.6),
    ]


def test_kpis_totals_match_the_python_they_replaced(session: ScopedSession) -> None:
    """Spend, value, margin and efficiency come out of SQL unchanged."""
    rows = _kpi_seed()
    _add(session, rows)
    session.commit()

    result = kpis(session)

    assert result["total_ai_spend_usd"] == pytest.approx(35.5, rel=_REL)
    assert result["total_ai_value_usd"] == pytest.approx(350.5, rel=_REL)
    assert result["net_ai_margin_usd"] == pytest.approx(315.0, rel=_REL)
    assert result["efficiency_index"] == pytest.approx(350.5 / 35.5, rel=_REL)


def test_kpis_confidence_is_the_average_of_the_clamped_per_row_scores(
    session: ScopedSession,
) -> None:
    """``avg(case(...))`` in SQL equals ``sum(...)/len(...)`` in Python."""
    rows = _kpi_seed()
    _add(session, rows)
    session.commit()

    expected = [_python_confidence(row) for row in rows]
    assert expected == pytest.approx([0.8, 0.8, 0.0, 0.8], rel=_REL)

    score = kpis(session)["portfolio_confidence_score"]

    assert score == pytest.approx(sum(expected) / len(expected), rel=_REL)
    assert score == pytest.approx(0.6, rel=_REL)


def test_kpis_efficiency_index_is_zero_when_nothing_was_spent(session: ScopedSession) -> None:
    """A portfolio with value but no spend reports zero, not a divide by zero."""
    session.add(_estimate("cap-a", value=100.0, cost=0.0))
    session.commit()

    assert kpis(session)["efficiency_index"] == 0.0


# --- leaderboards: bounded, and pointed at opposite ends ----------------------


def _margin_seed() -> list[ValueEstimate]:
    """Eight capabilities with distinct, deliberately unsorted net margins."""
    margins = [40.0, -90.0, 10.0, 70.0, -30.0, 55.0, -5.0, 25.0]
    return [
        _estimate(f"cap{index}", value=100.0, cost=10.0, margin=margin, confidence_level=0.5)
        for index, margin in enumerate(margins)
    ]


def test_top_creators_is_capped_at_the_leaderboard_bound(session: ScopedSession) -> None:
    """Eight capabilities in, LEADERBOARD_ROWS out."""
    _add(session, _margin_seed())
    session.commit()

    rows = top_creators(session)

    assert len(rows) == LEADERBOARD_ROWS
    assert [row["net_margin_usd"] for row in rows] == pytest.approx([70.0, 55.0, 40.0, 25.0, 10.0])


def test_capital_destroyers_is_the_opposite_end_not_the_bottom_of_the_top(
    session: ScopedSession,
) -> None:
    """The worst capability must appear; it used to be truncated away first.

    ``capital_destroyers`` re-sorted ``top_creators``'s already-truncated five,
    so the true minimum (-90) was unreachable and the panel showed the fifth-best
    capability as the worst one.
    """
    _add(session, _margin_seed())
    session.commit()

    rows = capital_destroyers(session)

    assert len(rows) == LEADERBOARD_ROWS
    assert rows[0]["capability_id"] == "cap1"
    assert [row["net_margin_usd"] for row in rows] == pytest.approx(
        [-90.0, -30.0, -5.0, 10.0, 25.0]
    )


def test_the_two_leaderboards_do_not_report_the_same_extreme(session: ScopedSession) -> None:
    """Best and worst are different capabilities once there are enough of them."""
    _add(session, _margin_seed())
    session.commit()

    assert (
        top_creators(session)[0]["capability_id"] != capital_destroyers(session)[0]["capability_id"]
    )


def test_a_leaderboard_row_sums_margin_and_averages_confidence(session: ScopedSession) -> None:
    """Several estimates for one capability collapse into one grouped row."""
    _add(
        session,
        [
            _estimate("cap-a", margin=10.0, confidence_level=0.4),
            _estimate("cap-a", margin=30.0, confidence_level=0.8),
        ],
    )
    session.commit()

    rows = top_creators(session)

    assert len(rows) == 1
    assert rows[0]["net_margin_usd"] == pytest.approx(40.0, rel=_REL)
    assert rows[0]["confidence"] == pytest.approx(0.6, rel=_REL)


# --- trends: bounded by TREND_POINTS, ordered by the id recency proxy ---------


def _seed_estimates(session: ScopedSession, count: int, *, capability_id: str = "cap") -> None:
    """Insert ``count`` estimates for one capability, oldest id first."""
    _add(
        session,
        [
            _estimate(
                capability_id,
                value=float(index + 1),
                cost=1.0,
                confidence_level=index / 1000.0,
                drift=float(index),
            )
            for index in range(count)
        ],
    )
    session.commit()


def test_confidence_trend_stops_at_the_trend_bound(session: ScopedSession) -> None:
    """More history than the chart plots is discarded by the database."""
    _seed_estimates(session, TREND_POINTS + 7)

    points = confidence_trend(session)

    assert len(points) == TREND_POINTS


def test_confidence_trend_keeps_the_newest_points_in_ascending_order(
    session: ScopedSession,
) -> None:
    """The retained window is the tail of history, still plotted oldest-first."""
    _seed_estimates(session, TREND_POINTS + 7)

    ys = [point["y"] for point in confidence_trend(session)]

    assert ys == sorted(ys), "the descending-limit read was not re-reversed"
    assert ys[0] == pytest.approx(7 / 1000.0, rel=_REL)
    assert ys[-1] == pytest.approx((TREND_POINTS + 6) / 1000.0, rel=_REL)


def test_efficiency_trend_stops_at_the_trend_bound(session: ScopedSession) -> None:
    """The efficiency chart reads through the same bounded helper."""
    _seed_estimates(session, TREND_POINTS + 7)

    points = efficiency_trend(session)

    assert len(points) == TREND_POINTS
    assert points[0]["y"] == pytest.approx(8.0, rel=_REL)


def test_the_recency_proxy_is_the_id_not_the_period(session: ScopedSession) -> None:
    """Insertion order, not ``period_end``, decides which points survive.

    ``_recent_estimates`` orders by ``id`` descending because the table has no
    ingest timestamp.  Pinned rather than assumed: rows whose ``period_end``
    runs backwards still come back in insertion order, so a later swap to a
    time column is a visible change rather than a silent one.
    """
    _add(
        session,
        [
            _estimate("cap", confidence_level=0.1, period_end=datetime(2026, 3, 1, tzinfo=UTC)),
            _estimate("cap", confidence_level=0.2, period_end=datetime(2026, 2, 1, tzinfo=UTC)),
            _estimate("cap", confidence_level=0.3, period_end=datetime(2026, 1, 1, tzinfo=UTC)),
        ],
    )
    session.commit()

    points = confidence_trend(session)

    assert [point["y"] for point in points] == pytest.approx([0.1, 0.2, 0.3])
    assert [point["x"] for point in points] == [
        "2026-03-01T00:00:00",
        "2026-02-01T00:00:00",
        "2026-01-01T00:00:00",
    ]


def test_calibration_trend_stops_at_the_trend_bound(session: ScopedSession) -> None:
    """Calibration history is capped and reversed the same way."""
    _add(
        session,
        [
            CalibrationMetric(
                tenant_id="default", period=f"p{index:04d}", capability_id="cap", mape=float(index)
            )
            for index in range(TREND_POINTS + 3)
        ],
    )
    session.commit()

    points = calibration_trend(session)

    assert len(points) == TREND_POINTS
    assert points[0]["x"] == "p0003"
    assert points[-1]["x"] == f"p{TREND_POINTS + 2:04d}"


def test_action_suppression_stops_at_the_trend_bound(session: ScopedSession) -> None:
    """Blocked-estimate markers are capped like every other series."""
    _seed_estimates(session, TREND_POINTS + 5)

    assert len(action_suppression(session)) == TREND_POINTS


def test_action_suppression_only_reports_estimates_that_failed_the_gate(
    session: ScopedSession,
) -> None:
    """A passing estimate contributes no suppression marker."""
    _add(
        session,
        [
            _estimate("cap", gate_passed=True),
            _estimate("cap", gate_passed=False),
            _estimate("cap", gate_passed=False),
        ],
    )
    session.commit()

    assert len(action_suppression(session)) == 2


def test_drift_timeline_stops_at_the_trend_bound(session: ScopedSession) -> None:
    """One capability's drift history is capped at TREND_POINTS."""
    _seed_estimates(session, TREND_POINTS + 5, capability_id="cap-a")

    points = drift_timeline(session, "cap-a")

    assert len(points) == TREND_POINTS
    assert points[0]["y"] == pytest.approx(5.0, rel=_REL)
    assert points[-1]["y"] == pytest.approx(float(TREND_POINTS + 4), rel=_REL)


def test_drift_timeline_is_scoped_to_the_requested_capability(session: ScopedSession) -> None:
    """Another capability's estimates never enter the series."""
    _add(session, [_estimate("cap-a", drift=1.0), _estimate("cap-b", drift=2.0)])
    session.commit()

    assert [point["y"] for point in drift_timeline(session, "cap-a")] == pytest.approx([1.0])


# --- timeline + comparison bounds ---------------------------------------------


def test_policy_timeline_stops_at_the_timeline_bound(session: ScopedSession) -> None:
    """A deployment with more policy history than the panel shows is truncated."""
    _add(session, [_policy_action(index) for index in range(TIMELINE_ROWS + 5)])
    session.commit()

    rows = policy_timeline(session)

    assert len(rows) == TIMELINE_ROWS
    assert rows[0]["capability_id"] == f"cap{TIMELINE_ROWS + 4}", "newest-first was not preserved"


def test_estimated_vs_ground_truth_cost_stops_at_the_comparison_bound(
    session: ScopedSession,
) -> None:
    """The cost comparison renders at most COMPARISON_ROWS estimates.

    Non-discriminating on length alone -- the pre-rewrite shape already sliced
    ``[:20]`` in Python after reading the whole table.  What the bound changed is
    where the truncation happens, which only the ground-truth join below can see.
    """
    _add(session, [_cost_estimate(f"cap{index}") for index in range(COMPARISON_ROWS + 5)])
    session.commit()

    assert len(estimated_vs_ground_truth_cost(session)) == COMPARISON_ROWS


def test_estimated_vs_ground_truth_cost_sums_only_the_matching_capability(
    session: ScopedSession,
) -> None:
    """Ground truth is summed per capability and never leaks across them."""
    _add(session, [_cost_estimate("cap-a", total=7.0), _cost_estimate("cap-b", total=3.0)])
    _add(
        session,
        [
            GroundTruthCost(
                tenant_id="default",
                period_start=_NOW,
                period_end=_NOW,
                capability_id="cap-a",
                component="llm",
                amount_usd=2.0,
            ),
            GroundTruthCost(
                tenant_id="default",
                period_start=_NOW,
                period_end=_NOW,
                capability_id="cap-a",
                component="infra",
                amount_usd=5.0,
            ),
            GroundTruthCost(
                tenant_id="default",
                period_start=_NOW,
                period_end=_NOW,
                capability_id="cap-b",
                component="llm",
                amount_usd=1.0,
            ),
        ],
    )
    session.commit()

    by_capability = {
        row["capability_id"]: row["ground_truth_cost_usd"]
        for row in estimated_vs_ground_truth_cost(session)
    }

    assert by_capability["cap-a"] == pytest.approx(7.0, rel=_REL)
    assert by_capability["cap-b"] == pytest.approx(1.0, rel=_REL)


def test_estimated_vs_ground_truth_cost_reports_zero_for_an_unmeasured_capability(
    session: ScopedSession,
) -> None:
    """A capability with no ground truth reports 0.0, not a missing key."""
    session.add(_cost_estimate("cap-a", total=7.0))
    session.commit()

    rows = estimated_vs_ground_truth_cost(session)

    assert rows[0]["ground_truth_cost_usd"] == 0.0


def test_implementation_compare_stops_at_ten_rows(session: ScopedSession) -> None:
    """The per-capability comparison keeps ten rows.

    Non-discriminating on length alone: the pre-rewrite shape read the whole
    capability's history and then sliced ``[:10]`` in Python.
    """
    _add(session, [_estimate("cap-a", implementation_id=f"impl{index}") for index in range(15)])
    session.add(_estimate("cap-b"))
    session.commit()

    rows = implementation_compare(session, "cap-a")

    assert len(rows) == 10
    assert {row["capability_id"] for row in rows} == {"cap-a"}
    assert rows[0]["implementation_id"] == "impl14", "newest-first was not preserved"


def test_implementation_compare_labels_a_portfolio_row(session: ScopedSession) -> None:
    """An estimate with no implementation is reported as the portfolio."""
    session.add(_estimate("cap-a", implementation_id=None))
    session.commit()

    assert implementation_compare(session, "cap-a")[0]["implementation_id"] == "portfolio"


# --- counting reads: gates and data quality -----------------------------------


def test_confidence_gate_status_counts_passed_and_blocked(session: ScopedSession) -> None:
    """Every estimate lands in exactly one of the two buckets."""
    _add(session, [_estimate("cap", gate_passed=True) for _ in range(2)])
    _add(session, [_estimate("cap", gate_passed=False) for _ in range(3)])
    session.commit()

    assert confidence_gate_status(session) == {"passed": 2, "blocked": 3}


def test_confidence_gate_status_over_an_empty_table_is_zero_not_null(
    session: ScopedSession,
) -> None:
    """``sum`` over no rows is SQL NULL; the coalesce turns it into 0.

    Without it the int() cast would raise on an empty deployment -- the state
    every fresh install starts in.
    """
    assert confidence_gate_status(session) == {"passed": 0, "blocked": 0}


def test_data_quality_mix_counts_each_grade(session: ScopedSession) -> None:
    """The three known grades are counted; an unknown grade is not folded in."""
    _add(session, [_cost_estimate("cap", quality="measured") for _ in range(3)])
    _add(session, [_cost_estimate("cap", quality="inferred") for _ in range(2)])
    session.add(_cost_estimate("cap", quality="mixed"))
    session.add(_cost_estimate("cap", quality="speculative"))
    session.commit()

    assert data_quality_mix(session) == {"measured": 3, "inferred": 2, "mixed": 1}


def test_data_quality_mix_over_an_empty_table_reports_every_grade_as_zero(
    session: ScopedSession,
) -> None:
    """A grade with no group in the result set reads as 0, not as absent."""
    assert data_quality_mix(session) == {"measured": 0, "inferred": 0, "mixed": 0}


# --- capability ranking: latest-per-capability without a full scan ------------


def test_capability_ranking_uses_the_latest_estimate_and_snapshot(
    session: ScopedSession,
) -> None:
    """Ranking reads one row per capability, the highest id of each."""
    _add(session, [Capability(id="cap-a", name="A"), Capability(id="cap-b", name="B")])
    _add(
        session,
        [
            _estimate("cap-a", margin=1.0),
            _estimate("cap-a", margin=90.0),
            _estimate("cap-b", margin=50.0),
        ],
    )
    _add(
        session,
        [
            PerformanceSnapshot(
                tenant_id="default",
                capability_id="cap-a",
                aer=1.0,
                risk_adjusted_return=0.0,
                operational_drag=0.0,
                rule_output="hold",
                captured_at=_NOW,
            ),
            PerformanceSnapshot(
                tenant_id="default",
                capability_id="cap-a",
                aer=9.0,
                risk_adjusted_return=0.0,
                operational_drag=0.0,
                rule_output="hold",
                captured_at=_NOW,
            ),
        ],
    )
    session.commit()

    rows = capability_ranking(session)

    assert [row["capability_id"] for row in rows] == ["cap-a", "cap-b"]
    assert rows[0]["net_margin_usd"] == pytest.approx(90.0, rel=_REL)
    assert rows[0]["aer"] == pytest.approx(9.0, rel=_REL)
    assert rows[1]["aer"] == 0.0


def test_capability_ranking_skips_a_capability_with_no_estimate(session: ScopedSession) -> None:
    """A capability that was never valued is not ranked."""
    _add(session, [Capability(id="cap-a", name="A"), Capability(id="cap-b", name="B")])
    session.add(_estimate("cap-a", margin=5.0))
    session.commit()

    assert [row["capability_id"] for row in capability_ranking(session)] == ["cap-a"]
