from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from release.live_evaluation.fault_control import (
    EvaluationFaultState,
    EvaluationConnectorFaultError,
    EvaluationFaultingMemoryResolver,
    EvaluationProviderFaultError,
    FaultingProviderAdapter,
    register_fault_control_routes,
)
from zeroth.runtime.agents.provider import ProviderRequest, ProviderResponse


def _request(campaign_id: str = "evaluation-studio-v1") -> ProviderRequest:
    return ProviderRequest(
        model_name="openai/gpt-4o-mini",
        messages=[{"role": "user", "content": "test"}],
        metadata={"runtime_context": {"campaign_id": campaign_id, "run_id": "run-1"}},
    )


class _Inner:
    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(self, request: ProviderRequest) -> ProviderResponse:
        self.calls += 1
        return ProviderResponse(content={"answer": "live"})


def test_fault_state_arms_and_consumes_exactly_once(tmp_path: Path) -> None:
    state = EvaluationFaultState(tmp_path / "faults.sqlite3")
    state.arm(
        campaign_id="evaluation-studio-v1",
        target="provider",
        mode="rate_limit",
        parameters={"status": 429},
    )

    fault = state.consume(campaign_id="evaluation-studio-v1", target="provider")

    assert fault is not None
    assert fault.mode == "rate_limit"
    assert fault.parameters == {"status": 429}
    assert state.consume(campaign_id="evaluation-studio-v1", target="provider") is None


def test_fault_state_rejects_overwrite_and_cross_campaign_consumption(tmp_path: Path) -> None:
    state = EvaluationFaultState(tmp_path / "faults.sqlite3")
    state.arm(
        campaign_id="evaluation-studio-v1",
        target="provider",
        mode="timeout",
        parameters={"after_ms": 0},
    )
    with pytest.raises(ValueError, match="already armed"):
        state.arm(
            campaign_id="evaluation-studio-v1",
            target="provider",
            mode="malformed_response",
        )

    assert state.consume(campaign_id="evaluation-other", target="provider") is None
    assert state.consume(campaign_id="evaluation-studio-v1", target="provider") is not None


@pytest.mark.parametrize("mode", ["rate_limit", "timeout", "invalid_secret_reference"])
async def test_provider_fault_prevents_real_call_and_is_explicitly_non_ambiguous(
    tmp_path: Path, mode: str
) -> None:
    state = EvaluationFaultState(tmp_path / "faults.sqlite3")
    state.arm(campaign_id="evaluation-studio-v1", target="provider", mode=mode)
    inner = _Inner()
    adapter = FaultingProviderAdapter(inner=inner, state=state)

    with pytest.raises(EvaluationProviderFaultError) as captured:
        await adapter.ainvoke(_request())

    assert captured.value.mode == mode
    assert captured.value.provider_call_attempted is False
    assert inner.calls == 0


async def test_malformed_response_is_local_once_then_delegates(tmp_path: Path) -> None:
    state = EvaluationFaultState(tmp_path / "faults.sqlite3")
    state.arm(
        campaign_id="evaluation-studio-v1",
        target="provider",
        mode="malformed_response",
    )
    inner = _Inner()
    adapter = FaultingProviderAdapter(inner=inner, state=state)

    malformed = await adapter.ainvoke(_request())
    normal = await adapter.ainvoke(_request())

    assert malformed.content == {"evaluation_malformed_response": True}
    assert malformed.metadata["evaluation_fault"] == "malformed_response"
    assert malformed.metadata["cache_hit"] is True
    assert normal.content == {"answer": "live"}
    assert inner.calls == 1


async def test_revision_limit_response_is_local_for_exactly_two_calls(tmp_path: Path) -> None:
    state = EvaluationFaultState(tmp_path / "faults.sqlite3")
    state.arm(
        campaign_id="evaluation-studio-v1",
        target="provider",
        mode="revision_required",
        parameters={"uses": 2},
    )
    inner = _Inner()
    adapter = FaultingProviderAdapter(inner=inner, state=state)

    first = await adapter.ainvoke(_request())
    second = await adapter.ainvoke(_request())
    normal = await adapter.ainvoke(_request())

    for response in (first, second):
        assert response.content["revision_required"] is True
        assert response.content["revision_count"] == 1
        assert response.metadata["cache_hit"] is True
        assert "provider_request_id" not in response.metadata
    assert normal.content == {"answer": "live"}
    assert inner.calls == 1


async def test_non_campaign_request_never_consumes_fault(tmp_path: Path) -> None:
    state = EvaluationFaultState(tmp_path / "faults.sqlite3")
    state.arm(
        campaign_id="evaluation-studio-v1",
        target="provider",
        mode="rate_limit",
    )
    inner = _Inner()
    adapter = FaultingProviderAdapter(inner=inner, state=state)

    response = await adapter.ainvoke(
        ProviderRequest(model_name="test", messages=[{"role": "user", "content": "x"}])
    )

    assert response.content == {"answer": "live"}
    assert inner.calls == 1
    assert state.consume(campaign_id="evaluation-studio-v1", target="provider") is not None


def test_local_fault_control_route_arms_shared_state_and_rejects_other_campaign(
    tmp_path: Path,
) -> None:
    state = EvaluationFaultState(tmp_path / "faults.sqlite3")
    app = FastAPI()
    register_fault_control_routes(
        app,
        state=state,
        campaign_id="evaluation-studio-v1",
    )

    with TestClient(app) as client:
        response = client.post(
            "/faults/arm",
            json={
                "campaign_id": "evaluation-studio-v1",
                "operation_id": "harness-op-1",
                "run_id": "server-response:run_id",
                "deterministic": True,
                "target": "provider",
                "mode": "rate_limit",
                "parameters": {"status": 429},
            },
        )
        wrong_campaign = client.post(
            "/faults/arm",
            json={
                "campaign_id": "evaluation-other",
                "operation_id": "harness-op-2",
                "run_id": "server-response:run_id",
                "deterministic": True,
                "target": "provider",
                "mode": "timeout",
                "parameters": {},
            },
        )

    assert response.status_code == 204
    fault_id = response.headers.get("X-Evaluation-Fault-ID")
    assert isinstance(fault_id, str) and len(fault_id) == 32
    assert wrong_campaign.status_code == 422
    consumed = state.consume(campaign_id="evaluation-studio-v1", target="provider")
    assert consumed is not None and consumed.fault_id == fault_id


async def test_connector_faults_are_campaign_scoped_one_shot_and_local(tmp_path: Path) -> None:
    class Connector:
        def __init__(self) -> None:
            self.searches = 0

        async def search(self, query, scope, *, target=None):
            self.searches += 1
            return ["live"]

    connector = Connector()

    class Resolver:
        async def resolve(self, refs, **kwargs):
            return [SimpleNamespace(connector=connector)]

    state = EvaluationFaultState(tmp_path / "faults.sqlite3")
    resolver = EvaluationFaultingMemoryResolver(inner=Resolver(), state=state)
    state.arm(
        campaign_id="evaluation-studio-v1",
        target="connector",
        mode="unavailable",
    )
    [binding] = await resolver.resolve(
        ["eval_chroma_v1"],
        runtime_context={"campaign_id": "evaluation-studio-v1"},
    )

    with pytest.raises(EvaluationConnectorFaultError, match="unavailable"):
        await binding.connector.search({"text": "q"}, "shared")
    assert connector.searches == 0
    assert await binding.connector.search({"text": "q"}, "shared") == ["live"]
    assert connector.searches == 1


async def test_retrieval_miss_returns_empty_without_touching_backend(tmp_path: Path) -> None:
    class Connector:
        async def search(self, query, scope, *, target=None):
            raise AssertionError("backend must not be called")

    class Resolver:
        async def resolve(self, refs, **kwargs):
            return [SimpleNamespace(connector=Connector())]

    state = EvaluationFaultState(tmp_path / "faults.sqlite3")
    state.arm(
        campaign_id="evaluation-studio-v1",
        target="connector",
        mode="retrieval_miss",
    )
    resolver = EvaluationFaultingMemoryResolver(inner=Resolver(), state=state)
    [binding] = await resolver.resolve(
        ["eval_chroma_v1"],
        runtime_context={"campaign_id": "evaluation-studio-v1"},
    )

    assert await binding.connector.search({"text": "q"}, "shared") == []
