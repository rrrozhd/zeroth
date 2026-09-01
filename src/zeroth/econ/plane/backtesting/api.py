"""Authenticated API for bounded managed model-change backtests."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException

from zeroth.econ.plane.auth.scoped import ScopedUserClaims as UserClaims
from zeroth.econ.plane.backtesting.executor import BacktestExecutor, ManagedBacktestExecutor
from zeroth.econ.plane.backtesting.schemas import (
    BacktestComputation,
    BacktestCreate,
    EconomicBacktest,
)
from zeroth.econ.plane.backtesting.service import (
    decide,
    evidence_gaps,
    find_backtest,
    list_backtests,
    request_digest,
    retain_backtest,
)
from zeroth.econ.plane.cloud.auth import get_cloud_scoped_db, require_cloud_roles
from zeroth.econ.plane.cloud.entitlements import EntitlementError, release_usage, reserve_usage
from zeroth.econ.plane.scoped_session import ScopedSession

router = APIRouter(tags=["economic-change-control"])


def get_backtest_executor() -> BacktestExecutor:
    return ManagedBacktestExecutor()


@router.post("/backtests", response_model=EconomicBacktest)
async def create_backtest(
    payload: BacktestCreate,
    db: ScopedSession = Depends(get_cloud_scoped_db),  # noqa: B008
    user: UserClaims = Depends(require_cloud_roles("Admin", "Analyst")),  # noqa: B008
    executor: BacktestExecutor = Depends(get_backtest_executor),  # noqa: B008
) -> EconomicBacktest:
    digest = request_digest(payload)
    existing = find_backtest(db, digest)
    if existing is not None:
        return existing

    now = datetime.now(UTC)
    gaps = evidence_gaps(payload)
    if gaps:
        report = decide(
            payload,
            BacktestComputation(reasons=gaps),
            digest=digest,
            evaluated_at=now,
        )
        return retain_backtest(db, report, evaluated_by=user.sub)

    reserved_calls = len(payload.cases) * 4
    try:
        reserved = reserve_usage(db, "backtest_calls", reserved_calls)
    except EntitlementError as exc:
        raise HTTPException(status_code=402, detail=exc.detail) from exc
    try:
        computation = await executor.execute(payload)
    except Exception:
        if reserved:
            release_usage(db, "backtest_calls", reserved_calls)
        raise
    if computation.provider_calls > reserved_calls:
        if reserved:
            release_usage(db, "backtest_calls", reserved_calls)
        raise RuntimeError("backtest executor exceeded its provider-call budget")
    if reserved and computation.provider_calls < reserved_calls:
        release_usage(db, "backtest_calls", reserved_calls - computation.provider_calls)
    report = decide(payload, computation, digest=digest, evaluated_at=now)
    return retain_backtest(db, report, evaluated_by=user.sub)


@router.get("/backtests", response_model=list[EconomicBacktest])
def backtest_history(
    limit: int = 50,
    db: ScopedSession = Depends(get_cloud_scoped_db),  # noqa: B008
    _user: UserClaims = Depends(  # noqa: B008
        require_cloud_roles("Admin", "Analyst", "Approver", "Viewer")
    ),
) -> list[EconomicBacktest]:
    return list_backtests(db, limit=limit)
