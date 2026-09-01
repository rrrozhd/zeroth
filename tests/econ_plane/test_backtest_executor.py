from __future__ import annotations

import pytest

from zeroth.econ.analytics.rightsizing import ModelOption
from zeroth.econ.plane.backtesting import executor as executor_module
from zeroth.econ.plane.backtesting.schemas import BacktestCreate
from zeroth.runtime.agents.provider import ProviderRequest, ProviderResponse


class _Provider:
    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []

    async def ainvoke(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        if request.output_model is not None:
            return ProviderResponse(content='{"score": 1.0, "rationale": "equivalent"}')
        return ProviderResponse(content='{"total": "12.50"}')


class _RegressingProvider(_Provider):
    def __init__(self) -> None:
        super().__init__()
        self._last_model = ""

    async def ainvoke(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        if request.output_model is None:
            self._last_model = request.model_name
            return ProviderResponse(content='{"total": "12.50"}')
        score = 0.0 if self._last_model.endswith("candidate") else 1.0
        return ProviderResponse(content=f'{{"score": {score}, "rationale": "measured"}}')


def _option(model: str, cost: float) -> ModelOption:
    return ModelOption(
        model=model,
        provider="openai",
        input_per_mtok_usd=cost,
        output_per_mtok_usd=cost,
        blended_per_mtok_usd=cost,
        savings_pct=0,
    )


@pytest.mark.asyncio
async def test_managed_executor_runs_bounded_incumbent_and_candidate_replays(monkeypatch) -> None:
    options = {
        "openai/incumbent": _option("incumbent", 10),
        "openai/candidate": _option("candidate", 2),
    }
    monkeypatch.setattr("zeroth.econ.analytics.rightsizing.describe", options.get)
    provider = _Provider()
    executor = executor_module.ManagedBacktestExecutor(provider=provider)
    payload = BacktestCreate.model_validate(
        {
            "workflow": "invoice-agent",
            "baseline_version": "v7",
            "node_id": "extract",
            "incumbent_model": "openai/incumbent",
            "instruction": "Extract invoice fields.",
            "candidate": {"model": "openai/candidate"},
            "cases": [
                {"id": str(index), "input": {"text": str(index)}, "expected": {"total": str(index)}}
                for index in range(5)
            ],
            "constraints": {"min_success_rate": 0.95},
        }
    )

    result = await executor.execute(payload)

    assert result.incumbent_success_rate == 1
    assert result.candidate_success_rate == 1
    assert result.savings_pct == 80
    assert result.provider_calls == 20
    assert len(provider.requests) == 20


@pytest.mark.asyncio
async def test_completed_quality_regression_is_decidable_not_inconclusive(monkeypatch) -> None:
    options = {
        "openai/incumbent": _option("incumbent", 10),
        "openai/candidate": _option("candidate", 2),
    }
    monkeypatch.setattr("zeroth.econ.analytics.rightsizing.describe", options.get)
    executor = executor_module.ManagedBacktestExecutor(provider=_RegressingProvider())
    payload = BacktestCreate.model_validate(
        {
            "workflow": "invoice-agent",
            "baseline_version": "v7",
            "node_id": "extract",
            "incumbent_model": "openai/incumbent",
            "instruction": "Extract invoice fields.",
            "candidate": {"model": "openai/candidate"},
            "cases": [
                {"id": str(index), "input": {"text": str(index)}, "expected": {"total": str(index)}}
                for index in range(5)
            ],
            "constraints": {"min_success_rate": 0.95},
        }
    )

    result = await executor.execute(payload)

    assert result.candidate_success_rate == 0
    assert result.reasons == []
