"""Self-contained demo seed for `zeroth-core seed-demo`.

Registers demo contracts, publishes a minimal single-agent graph, and
creates a deployment for it — everything the stock service entrypoint
needs so that a fresh install can serve a real run without writing any
Python. The agent runner itself is constructed at serve time by
``zeroth.core.agent_runtime.factory`` from the graph's own node data.

Contract classes live in this module so their ``module:Class`` paths
resolve inside the installed wheel.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from pydantic import BaseModel

from zeroth.core.contracts.registry import ContractRegistry, ContractVersionExistsError
from zeroth.core.deployments import Deployment, DeploymentService, SQLiteDeploymentRepository
from zeroth.core.graph.models import AgentNode, AgentNodeData, DisplayMetadata, Graph
from zeroth.core.graph.repository import GraphRepository
from zeroth.runtime.graph_validation import GraphValidator

if TYPE_CHECKING:
    from zeroth.platform.storage.database import AsyncDatabase

DEMO_GRAPH_ID = "demo-hello"
DEMO_INPUT_CONTRACT = "contract://demo-question"
DEMO_OUTPUT_CONTRACT = "contract://demo-answer"
DEFAULT_DEMO_MODEL = "openai/gpt-4o-mini"


class DemoQuestion(BaseModel):
    """Input contract for the demo deployment."""

    question: str


class DemoAnswer(BaseModel):
    """Output contract for the demo deployment."""

    answer: str


def build_hello_graph(model: str = DEFAULT_DEMO_MODEL) -> Graph:
    """A single-agent graph: question in, one-sentence answer out."""
    agent = AgentNode(
        node_id="agent",
        graph_version_ref=f"{DEMO_GRAPH_ID}@1",
        display=DisplayMetadata(title="Demo agent"),
        input_contract_ref=DEMO_INPUT_CONTRACT,
        output_contract_ref=DEMO_OUTPUT_CONTRACT,
        agent=AgentNodeData(
            instruction="Answer the user's question in one short sentence.",
            model_provider=model,
        ),
    )
    return Graph(
        graph_id=DEMO_GRAPH_ID,
        name="Zeroth Hello Demo",
        entry_step="agent",
        nodes=[agent],
        edges=[],
    )


async def seed_demo(
    database: AsyncDatabase,
    *,
    deployment_ref: str = "default",
    model: str = DEFAULT_DEMO_MODEL,
) -> Deployment:
    """Register contracts, publish the demo graph, and deploy it.

    Idempotent: re-running against a database that already holds the demo
    contracts or deployment reuses what exists instead of failing.
    """
    registry = ContractRegistry(database)
    for model_type, name in (
        (DemoQuestion, DEMO_INPUT_CONTRACT),
        (DemoAnswer, DEMO_OUTPUT_CONTRACT),
    ):
        if await registry.latest_version(name) == 0:
            with contextlib.suppress(ContractVersionExistsError):
                await registry.register(model_type, name=name)

    graph_repository = GraphRepository(
        database, validator=GraphValidator(contract_registry=registry)
    )
    deployment_repository = SQLiteDeploymentRepository(database)
    deployment_service = DeploymentService(
        graph_repository=graph_repository,
        deployment_repository=deployment_repository,
        contract_registry=registry,
    )

    existing = await deployment_repository.get(deployment_ref)
    if existing is not None and existing.graph_id == DEMO_GRAPH_ID:
        return existing

    graph = await graph_repository.get(DEMO_GRAPH_ID)
    if graph is None:
        saved = await graph_repository.create(build_hello_graph(model))
        published = await graph_repository.publish(saved.graph_id, saved.version)
    elif graph.status.value == "draft":
        published = await graph_repository.publish(graph.graph_id, graph.version)
    else:
        published = graph

    return await deployment_service.deploy(deployment_ref, published.graph_id, published.version)
