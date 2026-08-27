from __future__ import annotations

import pytest

from tests.orchestrator.test_retrieval_dispatch import (
    _RecordingConnector,
    _entries,
    _node,
    _orchestrator,
    _run_stub,
)
from zeroth.integrations.memory.governed.models import MemoryScope
from zeroth.integrations.memory.models import ConnectorManifest
from zeroth.integrations.memory.registry import InMemoryConnectorRegistry, MemoryConnectorResolver


def _resolver(
    connector: _RecordingConnector,
    *,
    connector_type: str,
) -> MemoryConnectorResolver:
    registry = InMemoryConnectorRegistry()
    registry.register(
        "docs",
        ConnectorManifest(connector_type=connector_type, scope=MemoryScope.SHARED),
        connector,
    )
    return MemoryConnectorResolver(registry=registry, workflow_name="retrieval-cost-test")


@pytest.mark.asyncio
@pytest.mark.parametrize("entries", [[], _entries()])
async def test_builtin_local_retrieval_is_authoritatively_measured_zero(entries) -> None:  # noqa: ANN001
    connector = _RecordingConnector(entries)

    _, audit = await _orchestrator(
        _resolver(connector, connector_type="ephemeral")
    )._dispatch_retrieval_node(
        _node(query_key="question", top_k=1, as_name="context"),
        _run_stub(),
        {"question": "provider-independent fixture"},
    )

    assert audit["retrieval"]["result_count"] == len(entries)
    assert audit["retrieval_result_count"] == len(entries)
    assert audit["cost_usd"] == 0.0
    assert audit["estimated_cost_usd"] == 0.0
    assert audit["cost_measurement"] == "measured"
    assert audit["provider_call_count"] == 0


@pytest.mark.asyncio
async def test_external_retrieval_cannot_claim_local_measured_zero() -> None:
    connector = _RecordingConnector([])

    _, audit = await _orchestrator(
        _resolver(connector, connector_type="chroma")
    )._dispatch_retrieval_node(
        _node(query_key="question", top_k=1, as_name="context"),
        _run_stub(),
        {"question": "provider-backed fixture"},
    )

    assert "cost_usd" not in audit
    assert "estimated_cost_usd" not in audit
    assert "cost_measurement" not in audit
    assert "provider_call_count" not in audit


@pytest.mark.asyncio
async def test_external_retrieval_promotes_its_embedding_settlement_exactly_once() -> None:
    connector = _RecordingConnector(_entries())

    class CostAwareResolver(MemoryConnectorResolver):
        async def consume_embedding_call_costs(
            self,
            *,
            tenant_id: str,
            run_id: str,
            node_id: str,
            campaign_id: str,
            operation: str,
        ) -> tuple[dict[str, object], ...]:
            assert (tenant_id, run_id, node_id, campaign_id, operation) == (
                "default",
                "r1",
                "retrieve",
                "campaign-a",
                "search",
            )
            return (
                {
                    "operation_id": "workflow-embedding:r1:retrieve:search:one",
                    "cost_event_id": "probe-cost-one",
                    "provider_request_id": "provider-request-one",
                    "estimated_cost_usd": 0.00000014,
                    "cost_measurement": "estimated",
                    "cleanup_status": "complete",
                },
            )

    registry = InMemoryConnectorRegistry()
    registry.register(
        "docs",
        ConnectorManifest(connector_type="chroma", scope=MemoryScope.SHARED),
        connector,
    )
    resolver = CostAwareResolver(registry=registry, workflow_name="retrieval-cost-test")
    run = _run_stub()
    run.metadata = {"campaign_id": "campaign-a"}

    _, audit = await _orchestrator(resolver)._dispatch_retrieval_node(
        _node(query_key="question", top_k=1, as_name="context"),
        run,
        {"question": "provider-backed fixture"},
    )

    assert audit["estimated_cost_usd"] == pytest.approx(0.00000014)
    assert audit["cost_measurement"] == "estimated"
    assert audit["provider_call_count"] == 1
    assert audit["cost_event_id"] == "probe-cost-one"
    assert audit["operation_id"] == "workflow-embedding:r1:retrieve:search:one"
    assert audit["provider_request_id"] == "provider-request-one"
    assert audit["cleanup_status"] == "complete"


@pytest.mark.asyncio
async def test_explicit_provider_free_external_retrieval_is_measured_zero() -> None:
    connector = _RecordingConnector([])
    registry = InMemoryConnectorRegistry()
    registry.register(
        "docs",
        ConnectorManifest(
            connector_type="chroma",
            scope=MemoryScope.SHARED,
            config={"provider_call_mode": "none"},
        ),
        connector,
    )

    _, audit = await _orchestrator(
        MemoryConnectorResolver(registry=registry, workflow_name="retrieval-cost-test")
    )._dispatch_retrieval_node(
        _node(query_key="question", top_k=1, as_name="context"),
        _run_stub(),
        {"question": "provider-independent fixture"},
    )

    assert audit["cost_usd"] == 0.0
    assert audit["estimated_cost_usd"] == 0.0
    assert audit["cost_measurement"] == "measured"
    assert audit["provider_call_count"] == 0
