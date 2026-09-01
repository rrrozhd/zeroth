"""Plane adapter for the provider-neutral economic backtest executor."""

from __future__ import annotations

from typing import Protocol

from zeroth.econ.analytics.rightsizing_experiment import (
    HostedBacktestCase,
    HostedBacktestRequest,
    HostedModelBacktest,
)
from zeroth.econ.plane.backtesting.schemas import BacktestComputation, BacktestCreate


class BacktestExecutor(Protocol):
    async def execute(self, payload: BacktestCreate) -> BacktestComputation: ...


class ManagedBacktestExecutor:
    """Replay one tool-free node against service-managed provider credentials."""

    def __init__(self, provider: object | None = None) -> None:
        self._engine = HostedModelBacktest(provider=provider)  # type: ignore[arg-type]

    async def execute(self, payload: BacktestCreate) -> BacktestComputation:
        candidate_ref = payload.candidate.get("model")
        if not isinstance(candidate_ref, str) or not candidate_ref.strip():
            return BacktestComputation(reasons=["candidate.model is required"])
        if payload.incumbent_model is None or payload.instruction is None:
            return BacktestComputation(reasons=["incumbent_model and instruction are required"])
        result = await self._engine.execute(
            HostedBacktestRequest(
                workflow=payload.workflow,
                node_id=payload.node_id,
                incumbent_model=payload.incumbent_model,
                candidate_model=candidate_ref,
                instruction=payload.instruction,
                cases=tuple(
                    HostedBacktestCase(id=case.id, input=case.input, expected=case.expected)
                    for case in payload.cases
                ),
            )
        )
        return BacktestComputation(
            incumbent_success_rate=result.incumbent_success_rate,
            candidate_success_rate=result.candidate_success_rate,
            candidate_error_rate=result.candidate_error_rate,
            savings_pct=result.savings_pct,
            provider_calls=result.provider_calls,
            reasons=result.reasons,
        )
