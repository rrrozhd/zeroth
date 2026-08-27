"""Provider-free fixture for the live context-window acceptance checkpoint.

The graph deliberately names the documented incumbent model so the ordinary
token counter and context-window implementation are exercised.  Its runner is
always supplied an in-process adapter, however, so this checkpoint cannot make
a priced provider call or require a provider credential.
"""

from __future__ import annotations

import contextlib
from typing import Literal

from pydantic import BaseModel

from zeroth.contracts.graph import (
    AgentNode,
    AgentNodeData,
    DisplayMetadata,
    Graph,
)
from zeroth.contracts.graph.models import ContextWindowSettings
from zeroth.contracts.graph.repository import GraphRepository
from zeroth.contracts.registry import ContractRegistry, contract_scope_context
from zeroth.contracts.registry.errors import ContractVersionExistsError
from zeroth.runtime.agents.provider import ProviderRequest, ProviderResponse
from zeroth.runtime.graph_validation import GraphValidator
from zeroth.service.deployments import Deployment, DeploymentService
from zeroth.service.deployments.repository import SQLiteDeploymentRepository

CONTEXT_TENANT_ID = "evaluation-context-v1"
CONTEXT_GRAPH_ID = "evaluation-context-compaction"
CONTEXT_DEPLOYMENT_REF = "evaluation-context-compaction-v1"
CONTEXT_INPUT_CONTRACT = "evaluation.context.messages"
CONTEXT_OUTPUT_CONTRACT = "evaluation.context.answer"


class ConversationTurn(BaseModel):
    """One explicitly typed conversation turn accepted by the fixture."""

    role: Literal["human", "ai", "tool"]
    content: str


class ContextMessagesInput(BaseModel):
    """Thread-continuation input used by the real run API."""

    messages: list[ConversationTurn]


class ContextAnswer(BaseModel):
    """Safe deterministic output returned by the provider-free adapter."""

    answer: str


class ProviderFreeContextAdapter:
    """Deterministic adapter that records invocations but never uses a network."""

    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []
        self.priced_calls_performed = 0

    async def ainvoke(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        return ProviderResponse(
            content='{"answer":"provider-free context checkpoint"}',
            cost_usd=0.0,
            metadata={"provider_request_id": f"provider-free-{len(self.requests)}"},
        )


def build_context_graph() -> Graph:
    """Return the single-agent graph used by the acceptance checkpoint."""
    node = AgentNode(
        node_id="research",
        graph_version_ref=f"{CONTEXT_GRAPH_ID}@1",
        display=DisplayMetadata(title="Compacted conversation"),
        input_contract_ref=CONTEXT_INPUT_CONTRACT,
        output_contract_ref=CONTEXT_OUTPUT_CONTRACT,
        agent=AgentNodeData(
            instruction="Return the deterministic context checkpoint answer.",
            model_provider="openai/gpt-4o-mini",
            state_persistence={"mode": "thread"},
            thread_participation="full",
            input_messages_key="messages",
            persist_conversation=True,
            conversation_max_turns=8,
            context_window=ContextWindowSettings(
                max_context_tokens=32,
                summary_trigger_ratio=0.5,
                compaction_strategy="truncation",
                preserve_recent_messages_count=2,
                archive_originals=True,
            ),
        ),
    )
    return Graph(
        graph_id=CONTEXT_GRAPH_ID,
        name="Provider-free context compaction checkpoint",
        tenant_id=CONTEXT_TENANT_ID,
        entry_step=node.node_id,
        nodes=[node],
        edges=[],
    )


async def seed_context_fixture(
    database,
    *,
    tenant_id: str = CONTEXT_TENANT_ID,
) -> Deployment:
    """Register, publish, and deploy the idempotent tenant-owned fixture."""
    registry = ContractRegistry.scoped(database, contract_scope_context(tenant_id, None))
    for model, name in (
        (ContextMessagesInput, CONTEXT_INPUT_CONTRACT),
        (ContextAnswer, CONTEXT_OUTPUT_CONTRACT),
    ):
        if await registry.latest_version(name) == 0:
            with contextlib.suppress(ContractVersionExistsError):
                await registry.register(model, name=name)

    graph_repository = GraphRepository(
        database,
        validator=GraphValidator(contract_registry=registry),
    )
    deployment_repository = SQLiteDeploymentRepository(database)
    existing = await deployment_repository.get(
        CONTEXT_DEPLOYMENT_REF,
        tenant_id=tenant_id,
        workspace_id=None,
    )
    if existing is not None and existing.graph_id == CONTEXT_GRAPH_ID:
        return existing

    graph = await graph_repository.get(
        CONTEXT_GRAPH_ID,
        tenant_id=tenant_id,
        workspace_id=None,
    )
    if graph is None:
        authored = build_context_graph().model_copy(update={"tenant_id": tenant_id})
        saved = await graph_repository.create(
            authored,
            tenant_id=tenant_id,
            workspace_id=None,
        )
        published = await graph_repository.publish(
            saved.graph_id,
            saved.version,
            tenant_id=tenant_id,
            workspace_id=None,
        )
    elif graph.status.value == "draft":
        published = await graph_repository.publish(
            graph.graph_id,
            graph.version,
            tenant_id=tenant_id,
            workspace_id=None,
        )
    else:
        published = graph

    return await DeploymentService(
        graph_repository=graph_repository,
        deployment_repository=deployment_repository,
        contract_registry=registry,
    ).deploy(
        CONTEXT_DEPLOYMENT_REF,
        published.graph_id,
        published.version,
        tenant_id=tenant_id,
        workspace_id=None,
    )
