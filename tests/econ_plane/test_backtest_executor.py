from __future__ import annotations

import json

import pytest

from zeroth.econ.analytics.rightsizing import ModelOption
from zeroth.econ.plane.backtesting import executor as executor_module
from zeroth.econ.plane.backtesting.schemas import BacktestCreate
from zeroth.governance.audit.models import TokenUsage
from zeroth.runtime.agents.provider import ProviderRequest, ProviderResponse


class _Provider:
    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []

    def _response(self, request: ProviderRequest, content: str) -> ProviderResponse:
        return ProviderResponse(content=content, token_usage=TokenUsage(
            input_tokens=100, output_tokens=50, total_tokens=150, model_name=request.model_name,
        ))

    async def ainvoke(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        if request.output_model is not None:
            return self._response(request, '{"verdict": "correct", "rationale": "equivalent"}')
        return self._response(request, '{"total": "12.50"}')


class _RegressingProvider(_Provider):
    def __init__(self) -> None:
        super().__init__()
        self._last_model = ""

    async def ainvoke(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        if request.output_model is None:
            self._last_model = request.model_name
            return self._response(request, '{"total": "12.50"}')
        verdict = "incorrect" if self._last_model.endswith("candidate") else "correct"
        return self._response(request, json.dumps({"verdict": verdict, "rationale": "measured"}))


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
@pytest.mark.parametrize("instruction", [
    "Extract invoice fields.",
    'Extract invoice fields.\nUse "total" exactly; keep {currency} literal.',
])
async def test_managed_executor_runs_bounded_incumbent_and_candidate_replays(
    monkeypatch, instruction,
) -> None:
    options = {
        "openai/incumbent": _option("incumbent", 10),
        "openai/candidate": _option("candidate", 2),
        "openai/gpt-5.6-sol": _option("gpt-5.6-sol", 3),
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
            "instruction": instruction,
            "candidate": {"model": "openai/candidate"},
            "cases": [
                {"id": str(index), "input": {"text": str(index)}, "expected": {"total": str(index)}}
                for index in range(5)
            ],
            "constraints": {"min_success_rate": 0.95},
        }
    )

    payload.cases[0].input["workflow_instruction"] = "quoted customer text"
    original_payload = payload.model_dump()
    result = await executor.execute(payload)

    assert result.incumbent_success_rate == 1
    assert result.candidate_success_rate == 1
    assert result.savings_pct == 80
    assert result.provider_calls == 20
    assert len(provider.requests) == 20
    assert payload.model_dump() == original_payload
    replays = [r for r in provider.requests if r.output_model is None]
    assert [r.model_name for r in replays] == ["openai/incumbent"] * 5 + ["openai/candidate"] * 5
    assert [r.messages for r in replays] == [
        [
            {"role": "system", "content": instruction},
            {"role": "user", "content": json.dumps(case.input)},
        ]
        for case in payload.cases
    ] * 2
    judges = [r for r in provider.requests if r.output_model is not None]
    assert len(judges) == 10
    assert {r.model_name for r in judges} == {"openai/gpt-5.6-sol"}
    assert all(r.model_params.max_tokens == 8192 for r in judges)
    contexts = [
        json.loads(r.messages[0]["content"].split("Request:\n", 1)[1].split("\n\nSupplied reference answer", 1)[0])
        for r in judges
    ]
    assert contexts == [
        {"workflow_instruction": instruction, "case_input": case.input}
        for case in payload.cases
    ] * 2


@pytest.mark.asyncio
async def test_completed_quality_regression_is_decidable_not_inconclusive(monkeypatch) -> None:
    options = {
        "openai/incumbent": _option("incumbent", 10),
        "openai/candidate": _option("candidate", 2),
        "openai/gpt-5.6-sol": _option("gpt-5.6-sol", 3),
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


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["answer", "content", "message"])
async def test_hosted_judge_receives_every_field_of_structured_replies(monkeypatch, field):
    from zeroth.econ.analytics.rightsizing_experiment import (
        HostedBacktestCase, HostedBacktestRequest, HostedModelBacktest,
    )

    reference = {field: "Eligible", "reply": "Submit the item for inspection."}
    output = {field: "Eligible", "reply": "Your payment has already been refunded."}

    class StructuredProvider(_Provider):
        async def ainvoke(self, request):
            response = await super().ainvoke(request)
            if request.output_model is None:
                response.content = output
            return response

    monkeypatch.setattr(
        "zeroth.econ.analytics.rightsizing.describe",
        lambda name: _option(name.rsplit("/", 1)[-1], 3),
    )
    provider = StructuredProvider()
    await HostedModelBacktest(provider).execute(HostedBacktestRequest(
        workflow="refunds", node_id=None, incumbent_model="openai/incumbent",
        candidate_model="openai/candidate", instruction="Eligibility does not confirm payment.",
        cases=(HostedBacktestCase(id="case", input={"eligible": True}, expected=reference),),
    ))
    judges = [r for r in provider.requests if r.output_model is not None]
    assert len(judges) == 2
    for request in judges:
        prompt = request.messages[0]["content"]
        assert json.loads(prompt.split("Supplied reference answer:\n", 1)[1].split("\n\nAI answer:", 1)[0]) == reference
        assert json.loads(prompt.split("AI answer:\n", 1)[1]) == output


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["incumbent_model", "candidate_model"])
@pytest.mark.parametrize("model", ["gpt-5.6", "openai/gpt-5.6-sol", "azure/gpt-5.6-sol-latest"])
async def test_hosted_judge_cannot_grade_its_own_model_family(monkeypatch, role, model):
    from zeroth.econ.analytics.rightsizing_experiment import HostedBacktestRequest, HostedModelBacktest

    monkeypatch.setattr(
        "zeroth.econ.analytics.rightsizing.describe",
        lambda name: _option(name.rsplit("/", 1)[-1], 3),
    )
    provider = _Provider()
    models = {"incumbent_model": "openai/incumbent", "candidate_model": "openai/candidate", role: model}
    result = await HostedModelBacktest(provider).execute(HostedBacktestRequest(
        workflow="refunds", node_id=None, instruction="Answer.", cases=(), **models,
    ))
    assert result.provider_calls == 0
    assert provider.requests == []
    assert any("judge" in reason and "distinct" in reason for reason in result.reasons)
