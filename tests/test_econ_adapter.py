"""Tests for InstrumentedProviderAdapter."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from zeroth.agent_runtime.provider import (
    DeterministicProviderAdapter,
    ProviderRequest,
    ProviderResponse,
)
from zeroth.audit.models import TokenUsage


@pytest.fixture
def token_usage():
    return TokenUsage(input_tokens=100, output_tokens=50, total_tokens=150, model_name="gpt-4o")


@pytest.fixture
def response_with_tokens(token_usage):
    return ProviderResponse(content="hello", token_usage=token_usage)


@pytest.fixture
def response_without_tokens():
    return ProviderResponse(content="hello")


@pytest.fixture
def mock_regulus_client():
    client = MagicMock()
    client.track_execution = MagicMock()
    return client


@pytest.fixture
def cost_estimator():
    from zeroth.econ.cost import CostEstimator

    return CostEstimator()


@pytest.fixture
def provider_request():
    return ProviderRequest(model_name="openai/gpt-4o", messages=[{"role": "user", "content": "hi"}])


async def test_adapter_enriches_response_with_cost(
    response_with_tokens, mock_regulus_client, cost_estimator, provider_request
):
    """InstrumentedProviderAdapter returns ProviderResponse with cost_usd and cost_event_id."""
    from zeroth.econ.adapter import InstrumentedProviderAdapter

    inner = DeterministicProviderAdapter([response_with_tokens])
    adapter = InstrumentedProviderAdapter(
        inner=inner,
        regulus_client=mock_regulus_client,
        cost_estimator=cost_estimator,
        node_id="test-node",
        run_id="run-1",
        tenant_id="tenant-1",
        deployment_ref="deploy-1",
    )
    result = await adapter.ainvoke(provider_request)
    assert result.cost_usd is not None
    assert result.cost_usd > 0
    assert result.cost_event_id is not None
    assert len(result.cost_event_id) > 0


async def test_adapter_calls_track_execution_with_correct_event(
    response_with_tokens, mock_regulus_client, cost_estimator, provider_request
):
    """InstrumentedProviderAdapter calls track_execution with correct ExecutionEvent fields."""
    from econ_instrumentation import ExecutionEvent

    from zeroth.econ.adapter import InstrumentedProviderAdapter

    inner = DeterministicProviderAdapter([response_with_tokens])
    adapter = InstrumentedProviderAdapter(
        inner=inner,
        regulus_client=mock_regulus_client,
        cost_estimator=cost_estimator,
        node_id="test-node",
        run_id="run-1",
        tenant_id="tenant-1",
        deployment_ref="deploy-1",
    )
    await adapter.ainvoke(provider_request)

    mock_regulus_client.track_execution.assert_called_once()
    event = mock_regulus_client.track_execution.call_args[0][0]
    assert isinstance(event, ExecutionEvent)
    assert event.capability_id == "test-node"
    assert event.implementation_id == "openai/gpt-4o"
    assert event.token_cost_usd > Decimal("0")
    assert event.metadata["run_id"] == "run-1"
    assert event.metadata["tenant_id"] == "tenant-1"
    assert event.metadata["deployment_ref"] == "deploy-1"
    assert event.metadata["input_tokens"] == 100
    assert event.metadata["output_tokens"] == 50
    assert event.metadata["total_tokens"] == 150


async def test_adapter_no_token_usage_defaults_to_zero(
    response_without_tokens, mock_regulus_client, cost_estimator, provider_request
):
    """When inner adapter returns no token_usage, cost_usd defaults to 0.0."""
    from zeroth.econ.adapter import InstrumentedProviderAdapter

    inner = DeterministicProviderAdapter([response_without_tokens])
    adapter = InstrumentedProviderAdapter(
        inner=inner,
        regulus_client=mock_regulus_client,
        cost_estimator=cost_estimator,
        node_id="test-node",
        run_id="run-1",
        tenant_id="tenant-1",
        deployment_ref="deploy-1",
    )
    result = await adapter.ainvoke(provider_request)
    assert result.cost_usd == 0.0
    # Event should still be emitted
    mock_regulus_client.track_execution.assert_called_once()


async def test_adapter_cost_estimator_error_defaults_to_zero(
    response_with_tokens, mock_regulus_client, provider_request
):
    """When CostEstimator raises, cost_usd defaults to 0.0 and event is still emitted."""
    from zeroth.econ.adapter import InstrumentedProviderAdapter

    broken_estimator = MagicMock()
    broken_estimator.estimate = MagicMock(side_effect=RuntimeError("broken"))

    inner = DeterministicProviderAdapter([response_with_tokens])
    adapter = InstrumentedProviderAdapter(
        inner=inner,
        regulus_client=mock_regulus_client,
        cost_estimator=broken_estimator,
        node_id="test-node",
        run_id="run-1",
        tenant_id="tenant-1",
        deployment_ref="deploy-1",
    )
    result = await adapter.ainvoke(provider_request)
    assert result.cost_usd == 0.0
    mock_regulus_client.track_execution.assert_called_once()


async def test_adapter_satisfies_provider_adapter_protocol():
    """InstrumentedProviderAdapter has ainvoke with correct signature (ProviderAdapter protocol)."""
    from zeroth.econ.adapter import InstrumentedProviderAdapter

    assert hasattr(InstrumentedProviderAdapter, "ainvoke")
    import inspect

    sig = inspect.signature(InstrumentedProviderAdapter.ainvoke)
    params = list(sig.parameters.keys())
    assert "request" in params


async def test_runner_copies_cost_fields_to_audit_record(
    response_with_tokens, mock_regulus_client, cost_estimator
):
    """Verify that ProviderResponse cost fields are accessible for audit record copying.

    The runner pattern (lines ~160-162) copies token_usage from response to audit.
    Similarly, cost_usd and cost_event_id should be copyable from the enriched response.
    """
    from zeroth.econ.adapter import InstrumentedProviderAdapter

    inner = DeterministicProviderAdapter([response_with_tokens])
    adapter = InstrumentedProviderAdapter(
        inner=inner,
        regulus_client=mock_regulus_client,
        cost_estimator=cost_estimator,
        node_id="test-node",
        run_id="run-1",
        tenant_id="tenant-1",
        deployment_ref="deploy-1",
    )
    request = ProviderRequest(
        model_name="openai/gpt-4o", messages=[{"role": "user", "content": "hi"}]
    )
    result = await adapter.ainvoke(provider_request)

    # Simulate what the runner would do: copy cost fields to a dict (audit record)
    record = {}
    if result.cost_usd is not None:
        record["cost_usd"] = result.cost_usd
    if result.cost_event_id is not None:
        record["cost_event_id"] = result.cost_event_id

    assert "cost_usd" in record
    assert "cost_event_id" in record
    assert record["cost_usd"] > 0
