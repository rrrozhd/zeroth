from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from zeroth.econ.measurement import MeasurementState
from zeroth.econ.plane.costing.models import (
    CalibrationMetric,
    CostEstimate,
    CostProfile,
    GroundTruthCost,
    PricingCatalog,
)
from zeroth.econ.plane.costing.schemas import CostProfileCreate, PricingCatalogCreate
from zeroth.econ.plane.instrumentation.models import ExecutionEvent
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.econ.plane.statistics.service import hierarchical_interval

#: Upper bound on the calibration history a summary read materialises.
CALIBRATION_SUMMARY_ROWS = 200


def _require_exact_scoped_session(db: object) -> ScopedSession:
    if type(db) is not ScopedSession:
        raise TypeError("costing persistence requires an exact ScopedSession")
    return db


def create_pricing_catalog(db: ScopedSession, payload: PricingCatalogCreate) -> PricingCatalog:
    db = _require_exact_scoped_session(db)
    row = PricingCatalog(**payload.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def create_cost_profile(db: ScopedSession, payload: CostProfileCreate) -> CostProfile:
    db = _require_exact_scoped_session(db)
    row = CostProfile(**payload.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def get_cost_profile(db: ScopedSession, profile_id: int) -> CostProfile | None:
    db = _require_exact_scoped_session(db)
    return db.get(CostProfile, profile_id)


class PricingCatalogReader:
    """Read-only global pricing view supplied to tenant costing operations."""

    __slots__ = ("_db",)

    def __init__(self, db: ScopedSession) -> None:
        db = _require_exact_scoped_session(db)
        if db.scope is not None:
            raise ValueError("pricing catalog reads require a global scope")
        self._db = db

    def lookup(self, provider: str, model: str, at: datetime) -> PricingCatalog | None:
        stmt = (
            select(PricingCatalog)
            .where(
                PricingCatalog.provider == provider,
                PricingCatalog.model == model,
                PricingCatalog.effective_from <= at,
            )
            .order_by(PricingCatalog.effective_from.desc())
        )
        rows = list(self._db.execute(stmt).scalars())
        for row in rows:
            if row.effective_to is None or row.effective_to >= at:
                return row
        return None


def estimate_cost_for_period(
    db: ScopedSession,
    capability_id: str,
    implementation_id: str | None,
    period_start: datetime,
    period_end: datetime,
    method_version: str = "v2_stat",
    *,
    pricing: PricingCatalogReader | None = None,
) -> CostEstimate:
    db = _require_exact_scoped_session(db)
    stmt = select(ExecutionEvent).where(
        ExecutionEvent.capability_id == capability_id,
        ExecutionEvent.timestamp >= period_start,
        ExecutionEvent.timestamp <= period_end,
    )
    if implementation_id:
        stmt = stmt.where(ExecutionEvent.implementation_id == implementation_id)
    executions = list(db.execute(stmt).scalars())

    measured_llm = 0.0
    measured_tool = 0.0
    measured_compute = 0.0
    has_measured = False
    inferred_samples: list[float] = []

    for e in executions:
        state = MeasurementState(e.cost_measurement)
        if state is MeasurementState.MEASURED:
            has_measured = True
            measured_llm += float(e.token_cost_usd or 0)
            measured_tool += float(e.tool_cost_usd or 0)
            measured_compute += float(e.compute_cost_usd or 0)
        elif state is MeasurementState.ESTIMATED:
            inferred_samples.append(
                float((e.token_cost_usd or 0) + (e.tool_cost_usd or 0) + (e.compute_cost_usd or 0))
            )

        md = e.event_metadata or {}
        provider = str(md.get("provider", ""))
        model = str(md.get("model", e.model_version))
        in_tokens = float(md.get("prompt_tokens", 0.0))
        out_tokens = float(md.get("completion_tokens", md.get("output_tokens", 0.0)))
        if (
            state is MeasurementState.UNMEASURED
            and provider
            and model
            and (in_tokens or out_tokens)
        ):
            if pricing is None:
                raise ValueError("inferred costing requires a global pricing catalog scope")
            price = pricing.lookup(provider, model, e.timestamp)
            if price:
                token_cost = (in_tokens / 1_000_000.0) * float(price.input_per_million_usd) + (
                    out_tokens / 1_000_000.0
                ) * float(price.output_per_million_usd)
                inferred_samples.append(token_cost)

    inferred_llm_mean, inferred_low, inferred_high = hierarchical_interval(
        inferred_samples, prior_mean=0.0
    )
    inferred_llm_total = sum(inferred_samples)

    llm_total = measured_llm + inferred_llm_total
    tool_total = measured_tool
    infra_total = measured_compute
    overhead_total = (llm_total + tool_total + infra_total) * 0.05
    total = llm_total + tool_total + infra_total + overhead_total

    data_quality = "unmeasured"
    if inferred_samples and has_measured:
        data_quality = "mixed"
    elif inferred_samples:
        data_quality = "inferred"
    elif executions and all(e.cost_measurement == "measured" for e in executions):
        data_quality = "measured"

    low = max(0.0, total - abs(inferred_high - inferred_llm_mean) * max(len(executions), 1))
    high = total + abs(inferred_high - inferred_llm_mean) * max(len(executions), 1)

    row = CostEstimate(
        execution_id=None,
        capability_id=capability_id,
        implementation_id=implementation_id,
        period_start=period_start,
        period_end=period_end,
        llm_cost_estimate_usd=llm_total,
        tool_cost_estimate_usd=tool_total,
        infra_cost_estimate_usd=infra_total,
        overhead_cost_estimate_usd=overhead_total,
        total_cost_estimate_usd=total,
        cost_interval_low_usd=low,
        cost_interval_high_usd=high,
        estimation_method="hierarchical_bayesian",
        data_quality=data_quality,
        method_version=method_version,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def latest_cost_estimate(db: ScopedSession, capability_id: str) -> CostEstimate | None:
    db = _require_exact_scoped_session(db)
    # ``.limit(1)`` is what makes "latest" a single row: without it the second
    # estimate recorded for a capability turns every read into
    # ``MultipleResultsFound``. The sibling services all carry the same bound.
    stmt = (
        select(CostEstimate)
        .where(CostEstimate.capability_id == capability_id)
        .order_by(CostEstimate.id.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def compute_calibration_summary(db: ScopedSession) -> list[CalibrationMetric]:
    db = _require_exact_scoped_session(db)
    # Lightweight daily aggregation scaffold for MVP; real reconciler can append rows.
    # Bounded like every other summary read: a calibration history grows one row
    # per reconciliation and nothing downstream renders more than a page of it.
    return list(
        db.execute(
            select(CalibrationMetric)
            .order_by(CalibrationMetric.id.desc())
            .limit(CALIBRATION_SUMMARY_ROWS)
        ).scalars()
    )


def add_ground_truth_rows(db: ScopedSession, rows: list[GroundTruthCost]) -> int:
    db = _require_exact_scoped_session(db)
    for row in rows:
        db.add(row)
    db.commit()
    return len(rows)
