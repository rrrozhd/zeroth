"""Digest, decision, and immutable persistence for hosted backtests."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import hmac
import json

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from zeroth.econ.plane.backtesting.models import EconomicBacktestRecord
from zeroth.econ.plane.backtesting.schemas import (
    BacktestComputation,
    BacktestCreate,
    EconomicBacktest,
)
from zeroth.econ.plane.cloud.models import CloudSubscription
from zeroth.econ.plane.config import settings
from zeroth.econ.plane.scoped_session import ScopedSession


def request_digest(payload: BacktestCreate) -> str:
    encoded = json.dumps(
        payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return hmac.new(settings.jwt_secret.encode(), encoded, hashlib.sha256).hexdigest()


def _stored(record: EconomicBacktestRecord) -> EconomicBacktest:
    evaluated_at = record.evaluated_at
    if evaluated_at.tzinfo is None:
        evaluated_at = evaluated_at.replace(tzinfo=UTC)
    return EconomicBacktest.model_validate(
        {
            **record.report_json,
            "backtest_id": record.backtest_id,
            "evaluated_at": evaluated_at,
        }
    )


def find_backtest(db: ScopedSession, digest: str) -> EconomicBacktest | None:
    record = db.scalars(
        select(EconomicBacktestRecord).where(EconomicBacktestRecord.request_digest == digest)
    ).one_or_none()
    return _stored(record) if record is not None else None


def evidence_gaps(payload: BacktestCreate) -> list[str]:
    gaps = []
    if not payload.cases:
        gaps.append("cases")
    elif len(payload.cases) < 5:
        gaps.append("at least 5 cases")
    for field in ("baseline_version", "node_id", "incumbent_model", "instruction"):
        if getattr(payload, field) is None:
            gaps.append(field)
    if not isinstance(payload.candidate.get("model"), str) or not payload.candidate.get("model"):
        gaps.append("candidate.model")
    if payload.constraints.max_cost_per_outcome_usd is not None:
        gaps.append("max_cost_per_outcome_usd is unsupported by isolated node replay")
    if payload.constraints.max_critical_error_rate is not None:
        gaps.append("max_critical_error_rate needs labeled critical-error evidence")
    return gaps


def decide(
    payload: BacktestCreate,
    computation: BacktestComputation,
    *,
    digest: str,
    evaluated_at: datetime,
) -> EconomicBacktest:
    reasons = list(computation.reasons)
    if computation.candidate_success_rate is None:
        reasons.append("candidate success rate is unavailable")
    failed = False
    minimum = payload.constraints.min_success_rate
    if minimum is not None and computation.candidate_success_rate is not None:
        if computation.candidate_success_rate < minimum:
            failed = True
            reasons.append("candidate success rate is below the required minimum")
    if computation.savings_pct is None:
        reasons.append("projected savings are unavailable")
    elif computation.savings_pct <= 0:
        failed = True
        reasons.append("candidate does not reduce projected model cost")

    if computation.reasons or computation.candidate_success_rate is None or computation.savings_pct is None:
        verdict = "abstain"
        action = "collect_evidence"
    elif failed:
        verdict = "fail"
        action = "keep_incumbent"
    else:
        verdict = "pass"
        action = "approve_candidate"
    candidate_model = payload.candidate.get("model")
    return EconomicBacktest(
        backtest_id="pending",
        request_digest=digest,
        workflow=payload.workflow,
        baseline_version=payload.baseline_version,
        node_id=payload.node_id,
        incumbent_model=payload.incumbent_model,
        candidate_model=candidate_model if isinstance(candidate_model, str) else None,
        verdict=verdict,
        recommended_action=action,
        cases=len(payload.cases),
        provider_call_credits=computation.provider_calls,
        incumbent_success_rate=computation.incumbent_success_rate,
        candidate_success_rate=computation.candidate_success_rate,
        candidate_error_rate=computation.candidate_error_rate,
        savings_pct=computation.savings_pct,
        constraints=payload.constraints,
        reasons=reasons,
        evaluated_at=evaluated_at,
    )


def retain_backtest(
    db: ScopedSession,
    report: EconomicBacktest,
    *,
    evaluated_by: str,
) -> EconomicBacktest:
    if type(db) is not ScopedSession or db.scope is None:
        raise TypeError("retained backtests require a tenant-scoped session")
    digest = report.request_digest
    backtest_id = f"bkt_{hashlib.sha256(f'{db.scope.tenant_id}:{digest}'.encode()).hexdigest()[:24]}"
    existing = db.get(EconomicBacktestRecord, backtest_id)
    if existing is not None:
        return _stored(existing)
    subscription = db.get(CloudSubscription, db.scope.tenant_id)
    period_start = subscription.period_start if subscription is not None else report.evaluated_at
    stored_report = report.model_dump(mode="json", exclude={"backtest_id", "evaluated_at"})
    record = EconomicBacktestRecord(
        backtest_id=backtest_id,
        tenant_id=db.scope.tenant_id,
        request_digest=digest,
        workflow=report.workflow,
        baseline_version=report.baseline_version,
        node_id=report.node_id,
        incumbent_model=report.incumbent_model,
        candidate_model=report.candidate_model,
        verdict=report.verdict,
        provider_call_credits=report.provider_call_credits,
        report_json=stored_report,
        period_start=period_start,
        evaluated_at=report.evaluated_at,
        evaluated_by=evaluated_by,
    )
    db.add(record)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        concurrent = db.get(EconomicBacktestRecord, backtest_id)
        if concurrent is None:
            raise
        return _stored(concurrent)
    return report.model_copy(update={"backtest_id": backtest_id})


def list_backtests(db: ScopedSession, *, limit: int = 50) -> list[EconomicBacktest]:
    records = list(
        db.scalars(
            select(EconomicBacktestRecord)
            .order_by(EconomicBacktestRecord.evaluated_at.desc())
            .limit(max(1, min(limit, 200)))
        )
    )
    return [_stored(record) for record in records]
