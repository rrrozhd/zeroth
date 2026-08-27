"""Create the tenant-owned bootstrap deployment required by a live campaign."""

from __future__ import annotations

import contextlib

from zeroth.contracts.graph.repository import GraphRepository
from zeroth.contracts.registry import ContractRegistry, contract_scope_context
from zeroth.contracts.registry.errors import ContractVersionExistsError
from zeroth.runtime.graph_validation import GraphValidator
from zeroth.service.demo import (
    DEFAULT_DEMO_MODEL,
    DEMO_INPUT_CONTRACT,
    DEMO_OUTPUT_CONTRACT,
    DemoAnswer,
    DemoQuestion,
    build_hello_graph,
)
from zeroth.service.deployments import Deployment, DeploymentService
from zeroth.service.deployments.repository import SQLiteDeploymentRepository


async def seed_campaign_bootstrap(
    database,
    *,
    tenant_id: str,
    deployment_ref: str,
    model: str = DEFAULT_DEMO_MODEL,
) -> Deployment:
    """Seed a minimal deployment owned by the campaign tenant.

    The stock demo seeder intentionally targets the compatibility/default tenant.
    Live evaluation uses this scoped variant so authoring endpoints cannot observe
    or mutate another tenant merely to obtain a serving bootstrap.
    """
    scoped_registry = ContractRegistry.scoped(
        database,
        contract_scope_context(tenant_id, None),
    )
    graph_id = f"{tenant_id}-bootstrap"
    for model_type, name in (
        (DemoQuestion, DEMO_INPUT_CONTRACT),
        (DemoAnswer, DEMO_OUTPUT_CONTRACT),
    ):
        if await scoped_registry.latest_version(name) == 0:
            with contextlib.suppress(ContractVersionExistsError):
                await scoped_registry.register(model_type, name=name)

    graph_repository = GraphRepository(
        database,
        validator=GraphValidator(contract_registry=scoped_registry),
    )
    deployment_repository = SQLiteDeploymentRepository(database)
    existing = await deployment_repository.get(
        deployment_ref,
        tenant_id=tenant_id,
        workspace_id=None,
    )
    if existing is not None and existing.graph_id == graph_id:
        return existing

    graph = await graph_repository.get(
        graph_id,
        tenant_id=tenant_id,
        workspace_id=None,
    )
    if graph is None:
        base_graph = build_hello_graph(model)
        campaign_graph = base_graph.model_copy(
            update={
                "graph_id": graph_id,
                "name": "Zeroth evaluation bootstrap",
                "nodes": [
                    node.model_copy(update={"graph_version_ref": f"{graph_id}@1"})
                    for node in base_graph.nodes
                ],
            }
        )
        saved = await graph_repository.create(
            campaign_graph,
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
        contract_registry=scoped_registry,
    ).deploy(
        deployment_ref,
        published.graph_id,
        published.version,
        tenant_id=tenant_id,
        workspace_id=None,
    )
