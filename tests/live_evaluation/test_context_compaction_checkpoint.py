from __future__ import annotations

from zeroth.contracts.graph import AgentNode
from zeroth.contracts.graph.serialization import hydrate_deployed_graph
from zeroth.governance.audit.evidence import build_summary
from zeroth.governance.audit.models import NodeAuditRecord
from zeroth.runtime.agents import RepositoryThreadStateStore
from zeroth.runtime.agents.provider import ProviderRequest

from release.live_evaluation.context_compaction_checkpoint import (
    CONTEXT_DEPLOYMENT_REF,
    ProviderFreeContextAdapter,
    seed_context_fixture,
)


async def test_context_fixture_is_provider_free_compacting_and_durable(sqlite_db) -> None:
    deployment = await seed_context_fixture(sqlite_db)
    graph = hydrate_deployed_graph(deployment)
    [node] = graph.nodes
    assert isinstance(node, AgentNode)
    assert node.agent.model_provider == "openai/gpt-4o-mini"
    assert node.agent.thread_participation == "full"
    assert node.agent.state_persistence == {"mode": "thread"}
    assert node.agent.input_messages_key == "messages"
    assert node.agent.persist_conversation is True
    assert node.agent.context_window is not None
    assert node.agent.context_window.compaction_strategy == "truncation"
    assert node.agent.context_window.archive_originals is True
    assert deployment.deployment_ref == CONTEXT_DEPLOYMENT_REF


async def test_context_fixture_runners_use_zero_cost_adapter_and_repository_state(
    sqlite_db,
) -> None:
    from zeroth.service.bootstrap.factory import build_runners_for_deployment

    await seed_context_fixture(sqlite_db)
    provider = ProviderFreeContextAdapter()
    runners = await build_runners_for_deployment(
        sqlite_db,
        CONTEXT_DEPLOYMENT_REF,
        tenant_id="evaluation-context-v1",
        provider=provider,
    )

    assert runners is not None
    assert runners["research"].provider is provider
    assert isinstance(runners["research"].thread_state_store, RepositoryThreadStateStore)
    assert provider.priced_calls_performed == 0

    response = await provider.ainvoke(ProviderRequest(model_name="openai/gpt-4o-mini", messages=[]))
    summary = build_summary(
        [
            NodeAuditRecord(
                audit_id="audit-provider-free",
                run_id="run-provider-free",
                node_id="research",
                graph_version_ref="evaluation-context-compaction@1",
                deployment_ref=CONTEXT_DEPLOYMENT_REF,
                tenant_id="evaluation-context-v1",
                status="completed",
                token_usage=response.token_usage,
                cost_usd=response.cost_usd,
                cost_event_id=response.cost_event_id,
            )
        ],
        [],
    )
    assert summary["priced_call_count"] == 0
    assert summary["cost_identity_state"] == "not_applicable_no_priced_call"
    assert summary["reconciliation_state"] == "reconciled_zero_activity"
