from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from zeroth.econ_plane.costing.models import CalibrationMetric, CostEstimate, CostProfile, GroundTruthCost, PricingCatalog
from zeroth.econ_plane.costing.schemas import CostProfileCreate, PricingCatalogCreate
from zeroth.econ_plane.instrumentation.models import ExecutionEvent
from zeroth.econ_plane.statistics.service import hierarchical_interval


def create_pricing_catalog(db: Session, payload: PricingCatalogCreate) -> PricingCatalog:
    row = PricingCatalog(**payload.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def create_cost_profile(db: Session, payload: CostProfileCreate) -> CostProfile:
    row = CostProfile(**payload.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def get_cost_profile(db: Session, profile_id: int) -> CostProfile | None:
    return db.get(CostProfile, profile_id)


def _lookup_pricing(db: Session, provider: str, model: str, at: datetime) -> PricingCatalog | None:
    stmt = (
        select(PricingCatalog)
        .where(PricingCatalog.provider == provider, PricingCatalog.model == model, PricingCatalog.effective_from <= at)
        .order_by(PricingCatalog.effective_from.desc())
    )
    rows = list(db.execute(stmt).scalars())
    for row in rows:
        if row.effective_to is None or row.effective_to >= at:
            return row
    return None


def estimate_cost_for_period(
    db: Session,
    capability_id: str,
    implementation_id: str | None,
    period_start: datetime,
    period_end: datetime,
    method_version: str = "v2_stat",
) -> CostEstimate:
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
    inferred_samples: list[float] = []

    for e in executions:
        measured_llm += float(e.token_cost_usd)
        measured_tool += float(e.tool_cost_usd)
        measured_compute += float(e.compute_cost_usd)

        md = e.event_metadata or {}
        provider = str(md.get("provider", ""))
        model = str(md.get("model", e.model_version))
        in_tokens = float(md.get("prompt_tokens", 0.0))
        out_tokens = float(md.get("completion_tokens", md.get("output_tokens", 0.0)))
        if (float(e.token_cost_usd) <= 0.0) and provider and model and (in_tokens or out_tokens):
            price = _lookup_pricing(db, provider, model, e.timestamp)
            if price:
                token_cost = (in_tokens / 1_000_000.0) * float(price.input_per_million_usd) + (
                    out_tokens / 1_000_000.0
                ) * float(price.output_per_million_usd)
                inferred_samples.append(token_cost)

    inferred_llm_mean, inferred_low, inferred_high = hierarchical_interval(inferred_samples, prior_mean=0.0)
    inferred_llm_total = inferred_llm_mean * max(len(executions), 1)

    llm_total = measured_llm if measured_llm > 0 else inferred_llm_total
    tool_total = measured_tool
    infra_total = measured_compute
    overhead_total = (llm_total + tool_total + infra_total) * 0.05
    total = llm_total + tool_total + infra_total + overhead_total

    data_quality = "measured"
    if measured_llm <= 0 and inferred_samples:
        data_quality = "inferred"
    elif measured_llm > 0 and inferred_samples:
        data_quality = "mixed"

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


def latest_cost_estimate(db: Session, capability_id: str) -> CostEstimate | None:
    stmt = select(CostEstimate).where(CostEstimate.capability_id == capability_id).order_by(CostEstimate.id.desc())
    return db.execute(stmt).scalar_one_or_none()


def compute_calibration_summary(db: Session) -> list[CalibrationMetric]:
    # Lightweight daily aggregation scaffold for MVP; real reconciler can append rows.
    return list(db.execute(select(CalibrationMetric).order_by(CalibrationMetric.id.desc())).scalars())


def add_ground_truth_rows(db: Session, rows: list[GroundTruthCost]) -> int:
    for row in rows:
        db.add(row)
    db.commit()
    return len(rows)
