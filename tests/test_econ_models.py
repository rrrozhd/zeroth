"""Tests for the econ module: models, client, cost estimator, and config integration."""

from __future__ import annotations

from decimal import Decimal


def test_regulus_settings_defaults():
    """RegulusSettings has correct defaults: enabled=False, base_url, budget_cache_ttl=30."""
    from zeroth.core.econ.models import RegulusSettings

    s = RegulusSettings()
    assert s.enabled is False
    assert s.base_url == "http://localhost:8000/v1"
    assert s.budget_cache_ttl == 30
    assert s.request_timeout == 5.0


def test_regulus_settings_accessible_via_zeroth_settings():
    """RegulusSettings is accessible via ZerothSettings().regulus."""
    from zeroth.core.config.settings import ZerothSettings

    settings = ZerothSettings()
    assert hasattr(settings, "regulus")
    assert settings.regulus.enabled is False
    assert settings.regulus.base_url == "http://localhost:8000/v1"


def test_cost_estimator_known_model():
    """CostEstimator.estimate() returns a Decimal for a known model."""
    from zeroth.core.econ.cost import CostEstimator

    estimator = CostEstimator()
    cost = estimator.estimate("openai/gpt-4o", input_tokens=100, output_tokens=50)
    assert isinstance(cost, Decimal)
    assert cost > Decimal("0")


def test_cost_estimator_unknown_model():
    """CostEstimator.estimate() returns Decimal('0') for an unknown model without raising."""
    from zeroth.core.econ.cost import CostEstimator

    estimator = CostEstimator()
    cost = estimator.estimate("unknown/nonexistent-model-xyz", input_tokens=100, output_tokens=50)
    assert isinstance(cost, Decimal)
    assert cost == Decimal("0")


def test_regulus_client_delegates_to_instrumentation_client():
    """RegulusClient.track_execution() delegates to InstrumentationClient.track_execution()."""
    from unittest.mock import MagicMock

    from econ_instrumentation import ExecutionEvent

    from zeroth.core.econ.client import RegulusClient

    mock_inner = MagicMock()
    client = RegulusClient.__new__(RegulusClient)
    client._client = mock_inner

    event = ExecutionEvent(capability_id="test-cap", implementation_id="test-impl")
    client.track_execution(event)
    mock_inner.track_execution.assert_called_once_with(event)


def test_provider_response_accepts_cost_fields():
    """ProviderResponse accepts cost_usd and cost_event_id as optional fields."""
    from zeroth.core.agent_runtime.provider import ProviderResponse

    resp = ProviderResponse(cost_usd=0.5, cost_event_id="evt-123")
    assert resp.cost_usd == 0.5
    assert resp.cost_event_id == "evt-123"

    # Defaults should be None
    resp2 = ProviderResponse()
    assert resp2.cost_usd is None
    assert resp2.cost_event_id is None


def test_node_audit_record_accepts_cost_fields():
    """NodeAuditRecord accepts cost_usd and cost_event_id as optional fields."""
    from zeroth.core.audit.models import NodeAuditRecord

    record = NodeAuditRecord(
        audit_id="a1",
        run_id="r1",
        node_id="n1",
        graph_version_ref="gv1",
        deployment_ref="d1",
        status="completed",
        cost_usd=0.5,
        cost_event_id="evt-123",
    )
    assert record.cost_usd == 0.5
    assert record.cost_event_id == "evt-123"

    # Defaults should be None
    record2 = NodeAuditRecord(
        audit_id="a2",
        run_id="r2",
        node_id="n2",
        graph_version_ref="gv2",
        deployment_ref="d2",
        status="completed",
    )
    assert record2.cost_usd is None
    assert record2.cost_event_id is None
