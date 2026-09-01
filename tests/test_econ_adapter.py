"""Tests for InstrumentedProviderAdapter."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock
import asyncio
from types import SimpleNamespace

import pytest

from zeroth.runtime.agents.provider import (
    DeterministicProviderAdapter,
    ProviderRequest,
    ProviderResponse,
)
from zeroth.governance.audit.models import TokenUsage
from zeroth.platform.measurement import MeasurementState


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
    from zeroth.econ.analytics.cost import CostEstimator

    return CostEstimator()


@pytest.fixture
def provider_request():
    return ProviderRequest(model_name="openai/gpt-4o", messages=[{"role": "user", "content": "hi"}])


async def test_adapter_enriches_response_with_cost(
    response_with_tokens, mock_regulus_client, cost_estimator, provider_request
):
    """InstrumentedProviderAdapter labels its local price as an estimate."""
    from zeroth.econ.analytics.adapter import InstrumentedProviderAdapter

    inner = DeterministicProviderAdapter([response_with_tokens])
    adapter = InstrumentedProviderAdapter(
        inner=inner,
        regulus_client=mock_regulus_client,
        cost_estimator=cost_estimator,
        node_id="test-node",
        run_id="run-1",
        tenant_id="tenant-1",
        deployment_ref="deploy-1",
        workflow_version="graph@7",
        subject_id="customer-42",
        dimensions={"plan": "enterprise", "region": "us-east"},
    )
    result = await adapter.ainvoke(provider_request)
    assert result.cost_usd is None
    assert result.estimated_cost_usd is not None
    assert result.estimated_cost_usd > 0
    assert result.cost_event_id is not None
    assert len(result.cost_event_id) > 0
    event = mock_regulus_client.track_execution.call_args.args[0]
    assert event.workflow_id == "deploy-1"
    assert event.workflow_version == "graph@7"
    assert event.run_id == "run-1"
    assert event.step_id == "test-node"
    assert event.attempt == 1
    assert event.subject_id == "customer-42"
    assert event.dimensions == {"plan": "enterprise", "region": "us-east"}


async def test_adapter_reserves_before_each_concurrent_workflow_provider_call(token_usage):
    from zeroth.econ.analytics.adapter import InstrumentedProviderAdapter

    class Reservations:
        def __init__(self) -> None:
            self.active: set[str] = set()
            self.committed: list[str] = []

        async def reserve_probe(self, **fields):
            assert fields["run_cap_usd"] == "0.25"
            self.active.add(fields["operation_id"])

        async def commit_probe(self, **fields):
            assert fields["operation_id"] in self.active
            self.committed.append(fields["operation_id"])
            return SimpleNamespace(
                cost_event_id=f"event:{fields['operation_id']}",
                cost_measurement="estimated",
                provider_request_id=None,
                cleanup_status="complete",
            )

    class Inner:
        async def ainvoke(self, request):
            await asyncio.sleep(0)
            return ProviderResponse(content="ok", token_usage=token_usage)

    reservations = Reservations()
    estimator = SimpleNamespace(estimate=lambda *args, **kwargs: Decimal("0.01"))
    adapter = InstrumentedProviderAdapter(
        inner=Inner(),
        regulus_client=None,
        cost_estimator=estimator,
        node_id="node-a",
        run_id="run-a",
        tenant_id="tenant-a",
        deployment_ref="deployment-a",
        cost_instrumentation=reservations,
        campaign_id="campaign-a",
        per_run_cap_usd=Decimal("0.25"),
        branch_id="branch-a",
    )

    results = await asyncio.gather(
        adapter.ainvoke(
            ProviderRequest(
                model_name="openai/gpt-4o-mini", messages=[{"role": "user", "content": "a"}]
            )
        ),
        adapter.ainvoke(
            ProviderRequest(
                model_name="openai/gpt-4o-mini", messages=[{"role": "user", "content": "b"}]
            )
        ),
    )

    assert len(set(reservations.committed)) == 2
    assert all("branch-a" in operation for operation in reservations.committed)
    assert {result.cost_event_id for result in results} == {
        f"event:{operation}" for operation in reservations.committed
    }
    evidence = adapter.call_evidence
    assert len(evidence) == 2
    assert {item.operation_id for item in evidence} == set(reservations.committed)
    assert {item.cost_event_id for item in evidence} == {
        f"event:{operation}" for operation in reservations.committed
    }
    assert {item.model_name for item in evidence} == {"openai/gpt-4o-mini"}
    assert all(item.estimated_cost_usd == Decimal("0.01") for item in evidence)
    assert all(item.measured_cost_usd is None for item in evidence)
    assert all(item.cleanup_status == "complete" for item in evidence)


async def test_adapter_retains_max_when_workflow_provider_outcome_is_ambiguous():
    from zeroth.econ.analytics.adapter import InstrumentedProviderAdapter

    class Reservations:
        def __init__(self) -> None:
            self.reserved = []
            self.ambiguous = []

        async def reserve_probe(self, **fields):
            self.reserved.append(fields)

        async def mark_probe_ambiguous(self, **fields):
            self.ambiguous.append(fields)

    class TimeoutInner:
        async def ainvoke(self, request):
            raise TimeoutError("provider outcome unknown")

    reservations = Reservations()
    adapter = InstrumentedProviderAdapter(
        inner=TimeoutInner(),
        regulus_client=None,
        cost_estimator=SimpleNamespace(estimate=lambda *args, **kwargs: Decimal("0.01")),
        node_id="node-a",
        run_id="run-a",
        tenant_id="tenant-a",
        deployment_ref="deployment-a",
        cost_instrumentation=reservations,
        campaign_id="campaign-a",
        per_run_cap_usd=Decimal("0.25"),
    )

    with pytest.raises(TimeoutError):
        await adapter.ainvoke(
            ProviderRequest(
                model_name="openai/gpt-4o-mini", messages=[{"role": "user", "content": "a"}]
            )
        )

    assert len(reservations.reserved) == 1
    assert reservations.ambiguous[0]["operation_id"] == reservations.reserved[0]["operation_id"]


async def test_adapter_releases_reservation_when_local_fault_prevents_provider_call():
    from zeroth.econ.analytics.adapter import InstrumentedProviderAdapter

    class Reservations:
        def __init__(self) -> None:
            self.reserved = []
            self.released = []
            self.ambiguous = []

        async def reserve_probe(self, **fields):
            self.reserved.append(fields)

        async def release_probe(self, **fields):
            self.released.append(fields)

        async def mark_probe_ambiguous(self, **fields):
            self.ambiguous.append(fields)

    class PreventedError(RuntimeError):
        provider_call_attempted = False

    class LocallyFaultedInner:
        async def ainvoke(self, request):
            raise PreventedError("deterministic local rate limit")

    reservations = Reservations()
    adapter = InstrumentedProviderAdapter(
        inner=LocallyFaultedInner(),
        regulus_client=None,
        cost_estimator=SimpleNamespace(estimate=lambda *args, **kwargs: Decimal("0.01")),
        node_id="node-a",
        run_id="run-a",
        tenant_id="tenant-a",
        deployment_ref="deployment-a",
        cost_instrumentation=reservations,
        campaign_id="campaign-a",
        per_run_cap_usd=Decimal("0.25"),
    )

    with pytest.raises(PreventedError):
        await adapter.ainvoke(
            ProviderRequest(
                model_name="openai/gpt-4o-mini",
                messages=[{"role": "user", "content": "a"}],
            )
        )

    assert len(reservations.reserved) == 1
    assert reservations.released[0]["operation_id"] == reservations.reserved[0]["operation_id"]
    assert reservations.released[0]["cleanup_status"] == "provider_not_called"
    assert reservations.ambiguous == []


async def test_adapter_without_regulus_stamps_cost_but_emits_no_event(
    response_with_tokens, cost_estimator, provider_request
):
    """regulus_client=None: cost is still attributed locally (litellm), no event emitted.

    This is the on-by-default path — cost tracking works without a Regulus backend.
    Proves the only client dereference (track_execution) is guarded.
    """
    from zeroth.econ.analytics.adapter import InstrumentedProviderAdapter

    inner = DeterministicProviderAdapter([response_with_tokens])
    adapter = InstrumentedProviderAdapter(
        inner=inner,
        regulus_client=None,
        cost_estimator=cost_estimator,
        node_id="test-node",
        run_id="run-1",
        tenant_id="tenant-1",
        deployment_ref="deploy-1",
    )
    result = await adapter.ainvoke(provider_request)
    assert result.cost_usd is None
    assert result.estimated_cost_usd is not None and result.estimated_cost_usd > 0
    assert result.cost_event_id is None  # no Regulus event to reference


@pytest.mark.parametrize("with_regulus", [False, True])
async def test_adapter_preserves_inner_measured_event(
    with_regulus, cost_estimator, provider_request
):
    from zeroth.econ.analytics.adapter import InstrumentedProviderAdapter

    client = MagicMock() if with_regulus else None
    measured = ProviderResponse(
        content="hello",
        token_usage=TokenUsage(
            input_tokens=100, output_tokens=50, total_tokens=150, model_name="gpt-4o"
        ),
        cost_usd=0.4,
        cost_measurement=MeasurementState.MEASURED,
        cost_event_id="inner-event",
    )
    adapter = InstrumentedProviderAdapter(
        inner=DeterministicProviderAdapter([measured]),
        regulus_client=client,
        cost_estimator=cost_estimator,
        node_id="test-node",
        run_id="run-1",
        tenant_id="tenant-1",
        deployment_ref="deploy-1",
    )

    result = await adapter.ainvoke(provider_request)

    assert result.cost_usd == 0.4
    assert result.estimated_cost_usd is None
    assert result.cost_measurement is MeasurementState.MEASURED
    assert result.cost_event_id == "inner-event"
    if client is not None:
        client.track_execution.assert_not_called()


async def test_adapter_calls_track_execution_with_correct_event(
    response_with_tokens, mock_regulus_client, cost_estimator, provider_request
):
    """InstrumentedProviderAdapter calls track_execution with correct ExecutionEvent fields."""
    from zeroth.econ.instrumentation import ExecutionEvent

    from zeroth.econ.analytics.adapter import InstrumentedProviderAdapter

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
    from zeroth.econ.analytics.identity import capability_identity, implementation_identity

    expected_capability = capability_identity("tenant-1", "deploy-1", "test-node")
    assert event.capability_id == expected_capability
    assert event.implementation_id == implementation_identity(expected_capability, "openai/gpt-4o")
    assert event.token_cost_usd > Decimal("0")
    assert event.metadata["run_id"] == "run-1"
    assert event.metadata["tenant_id"] == "tenant-1"
    assert event.metadata["deployment_ref"] == "deploy-1"
    assert event.metadata["input_tokens"] == 100
    assert event.metadata["output_tokens"] == 50
    assert event.metadata["total_tokens"] == 150


async def test_adapter_no_token_usage_stays_unmeasured(
    response_without_tokens, mock_regulus_client, cost_estimator, provider_request
):
    """Missing usage cannot be priced as a measured zero."""
    from zeroth.econ.analytics.adapter import InstrumentedProviderAdapter

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
    assert result.cost_usd is None
    assert result.estimated_cost_usd is None
    event = mock_regulus_client.track_execution.call_args.args[0]
    assert event.usage_measurement is MeasurementState.UNMEASURED
    assert event.metadata["input_tokens"] is None
    assert event.metadata["output_tokens"] is None
    assert event.metadata["total_tokens"] is None


async def test_adapter_cost_estimator_error_stays_unmeasured(
    response_with_tokens, mock_regulus_client, provider_request
):
    """A failed estimator cannot fabricate a measured zero."""
    from zeroth.econ.analytics.adapter import InstrumentedProviderAdapter

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
    assert result.cost_usd is None
    assert result.estimated_cost_usd is None
    mock_regulus_client.track_execution.assert_called_once()


async def test_adapter_satisfies_provider_adapter_protocol():
    """InstrumentedProviderAdapter has ainvoke with correct signature (ProviderAdapter protocol)."""
    from zeroth.econ.analytics.adapter import InstrumentedProviderAdapter

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
    from zeroth.econ.analytics.adapter import InstrumentedProviderAdapter

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
    req = ProviderRequest(model_name="openai/gpt-4o", messages=[{"role": "user", "content": "hi"}])
    result = await adapter.ainvoke(req)

    # Simulate what the runner would do: copy cost fields to a dict (audit record)
    record = {}
    if result.estimated_cost_usd is not None:
        record["estimated_cost_usd"] = result.estimated_cost_usd
    if result.cost_event_id is not None:
        record["cost_event_id"] = result.cost_event_id

    assert "estimated_cost_usd" in record
    assert "cost_event_id" in record
    assert record["estimated_cost_usd"] > 0


async def test_cache_hit_attributes_zero_cost_and_emits_no_event(mock_regulus_client):
    """A cache hit attributes zero marginal cost and emits no Regulus event.

    The orchestrator wraps the runner's provider with InstrumentedProviderAdapter as
    the *outermost* adapter, so an inner CachingProviderAdapter's hit still flows
    through this adapter. Without the cache-hit short-circuit, every hit would
    re-estimate cost on the cached tokens, stamp a fresh cost_usd, and fire a
    duplicate ExecutionEvent for a call that never reached a model.
    """
    from zeroth.runtime.agents.provider import CallableProviderAdapter
    from zeroth.runtime.agents.resilience import CachingProviderAdapter
    from zeroth.econ.analytics.adapter import InstrumentedProviderAdapter
    from zeroth.econ.analytics.cost import CostEstimator

    calls = {"n": 0}
    usage = TokenUsage(
        input_tokens=1000, output_tokens=500, total_tokens=1500, model_name="openai/gpt-4o-mini"
    )

    def _model(_request):
        calls["n"] += 1
        return ProviderResponse(content="hello", token_usage=usage)

    adapter = InstrumentedProviderAdapter(
        inner=CachingProviderAdapter(CallableProviderAdapter(_model)),
        regulus_client=mock_regulus_client,
        cost_estimator=CostEstimator(),
        node_id="test-node",
        run_id="run-1",
        tenant_id="tenant-1",
        deployment_ref="deploy-1",
    )
    request = ProviderRequest(
        model_name="openai/gpt-4o-mini", messages=[{"role": "user", "content": "hi"}]
    )

    first = await adapter.ainvoke(request)
    second = await adapter.ainvoke(request)

    # The model ran once; the second call was served from cache.
    assert calls["n"] == 1
    assert first.metadata.get("cache_hit") is False
    assert second.metadata.get("cache_hit") is True

    # Cold miss: real cost attributed and a Regulus event emitted.
    assert first.cost_usd is None
    assert first.estimated_cost_usd is not None and first.estimated_cost_usd > 0
    assert first.cost_event_id is not None

    # Cache hit: zero marginal cost, no new event, avoided spend recorded.
    assert second.cost_usd == 0.0
    assert second.cost_event_id is None
    assert second.metadata.get("cache_saved_usd", 0.0) > 0

    # Exactly one event total -- the cold miss, not the hit.
    mock_regulus_client.track_execution.assert_called_once()
