"""Deletion guards for template references in immutable graph state."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.service.helpers import agent_graph
from zeroth.contracts.graph import GraphStatus
from zeroth.contracts.graph.serialization import serialize_graph
from zeroth.contracts.templates import TemplateReference
from zeroth.contracts.templates.errors import TemplateNotFoundError
from zeroth.service.templates import DatabaseTemplateRegistry
from zeroth.service.templates.dependencies import (
    TemplateDependencyChecker,
    TemplateReferenceIndex,
)
from zeroth.contracts.graph import GraphRepository
from zeroth.service.deployments import DeploymentService, SQLiteDeploymentRepository


def _graph_with_template(
    *,
    graph_id: str,
    status: GraphStatus,
    template_version: int | None,
):
    graph = agent_graph(graph_id=graph_id)
    node = graph.nodes[0]
    node = node.model_copy(
        update={
            "agent": node.agent.model_copy(
                update={
                    "template_ref": TemplateReference(
                        name="grounded-answer",
                        version=template_version,
                    )
                }
            )
        }
    )
    return graph.model_copy(update={"nodes": [node], "status": status})


@pytest.mark.asyncio
async def test_explicit_reference_in_published_graph_blocks_matching_version() -> None:
    graph = _graph_with_template(
        graph_id="published-graph",
        status=GraphStatus.PUBLISHED,
        template_version=2,
    )
    graph_repository = AsyncMock()
    graph_repository.list.return_value = [graph]
    graph_repository.list_versions.return_value = [graph]
    deployment_service = AsyncMock()
    deployment_service.list.return_value = []
    checker = TemplateDependencyChecker(graph_repository, deployment_service)

    conflict = await checker.find_conflict(
        name="grounded-answer",
        version=2,
        is_latest=False,
        tenant_id="tenant-a",
        workspace_id="workspace-a",
    )

    assert conflict is not None
    assert conflict.source_kind == "published_graph"
    assert conflict.source_ref == "published-graph@1"
    graph_repository.list.assert_awaited_once_with(
        tenant_id="tenant-a", workspace_id="workspace-a"
    )
    deployment_service.list.assert_awaited_once_with(
        tenant_id="tenant-a", workspace_id="workspace-a"
    )


@pytest.mark.asyncio
async def test_latest_reference_only_blocks_deleting_current_latest_version() -> None:
    graph = _graph_with_template(
        graph_id="latest-graph",
        status=GraphStatus.PUBLISHED,
        template_version=None,
    )
    graph_repository = AsyncMock()
    graph_repository.list.return_value = [graph]
    graph_repository.list_versions.return_value = [graph]
    deployment_service = AsyncMock()
    deployment_service.list.return_value = []
    checker = TemplateDependencyChecker(graph_repository, deployment_service)

    older = await checker.find_conflict(
        name="grounded-answer",
        version=1,
        is_latest=False,
        tenant_id="tenant-a",
        workspace_id=None,
    )
    latest = await checker.find_conflict(
        name="grounded-answer",
        version=2,
        is_latest=True,
        tenant_id="tenant-a",
        workspace_id=None,
    )

    assert older is None
    assert latest is not None
    assert latest.reference_mode == "latest"


@pytest.mark.asyncio
async def test_deployed_snapshot_blocks_reference_after_source_graph_is_archived() -> None:
    archived = _graph_with_template(
        graph_id="archived-graph",
        status=GraphStatus.ARCHIVED,
        template_version=3,
    )
    graph_repository = AsyncMock()
    graph_repository.list.return_value = [archived]
    graph_repository.list_versions.return_value = [archived]
    deployment_service = AsyncMock()
    deployment_service.list.return_value = [
        SimpleNamespace(
            deployment_ref="deployment-a",
            version=4,
            serialized_graph=serialize_graph(archived),
        )
    ]
    checker = TemplateDependencyChecker(graph_repository, deployment_service)

    conflict = await checker.find_conflict(
        name="grounded-answer",
        version=3,
        is_latest=False,
        tenant_id="tenant-a",
        workspace_id=None,
    )

    assert conflict is not None
    assert conflict.source_kind == "deployment"
    assert conflict.source_ref == "deployment-a@4"


@pytest.mark.asyncio
async def test_unpublished_graph_does_not_block_deletion() -> None:
    draft = _graph_with_template(
        graph_id="draft-graph",
        status=GraphStatus.DRAFT,
        template_version=1,
    )
    graph_repository = AsyncMock()
    graph_repository.list.return_value = [draft]
    graph_repository.list_versions.return_value = [draft]
    deployment_service = AsyncMock()
    deployment_service.list.return_value = []
    checker = TemplateDependencyChecker(graph_repository, deployment_service)

    conflict = await checker.find_conflict(
        name="grounded-answer",
        version=1,
        is_latest=True,
        tenant_id="tenant-a",
        workspace_id=None,
    )

    assert conflict is None


@pytest.mark.asyncio
async def test_published_graph_reference_is_persisted_in_transactional_index(
    sqlite_db,
) -> None:
    index = TemplateReferenceIndex(
        sqlite_db,
        tenant_id="tenant-a",
        workspace_id="workspace-a",
    )
    registry = DatabaseTemplateRegistry(
        sqlite_db,
        tenant_id="tenant-a",
        workspace_id="workspace-a",
    )
    repository = GraphRepository(sqlite_db, template_reference_index=index)
    await registry.register("grounded-answer", 2, "Answer {{ input.question }}")
    draft = _graph_with_template(
        graph_id="indexed-graph",
        status=GraphStatus.DRAFT,
        template_version=2,
    ).model_copy(update={"tenant_id": "tenant-a", "workspace_id": "workspace-a"})
    await repository.create(
        draft,
        tenant_id="tenant-a",
        workspace_id="workspace-a",
    )

    await repository.publish(
        draft.graph_id,
        draft.version,
        tenant_id="tenant-a",
        workspace_id="workspace-a",
    )

    conflict = await index.find_conflict(
        name="grounded-answer",
        version=2,
        is_latest=True,
    )
    assert conflict is not None
    assert conflict.source_kind == "published_graph"
    assert conflict.source_ref == "indexed-graph@1"
    restored = TemplateReferenceIndex(
        sqlite_db,
        tenant_id="tenant-a",
        workspace_id="workspace-a",
    )
    assert await restored.find_conflict(
        name="grounded-answer",
        version=2,
        is_latest=True,
    ) == conflict


@pytest.mark.asyncio
async def test_publish_after_template_delete_cannot_create_dangling_reference(
    sqlite_db,
) -> None:
    index = TemplateReferenceIndex(
        sqlite_db,
        tenant_id="tenant-a",
        workspace_id=None,
    )
    registry = DatabaseTemplateRegistry(
        sqlite_db,
        tenant_id="tenant-a",
        workspace_id=None,
    )
    repository = GraphRepository(sqlite_db, template_reference_index=index)
    await registry.register("grounded-answer", 2, "Answer {{ input.question }}")
    draft = _graph_with_template(
        graph_id="racing-graph",
        status=GraphStatus.DRAFT,
        template_version=2,
    ).model_copy(update={"tenant_id": "tenant-a", "workspace_id": None})
    await repository.create(draft, tenant_id="tenant-a", workspace_id=None)
    await registry.delete("grounded-answer", 2)

    with pytest.raises(TemplateNotFoundError):
        await repository.publish(
            draft.graph_id,
            draft.version,
            tenant_id="tenant-a",
            workspace_id=None,
        )

    restored = await repository.get(
        draft.graph_id,
        draft.version,
        tenant_id="tenant-a",
        workspace_id=None,
    )
    assert restored is not None
    assert restored.status is GraphStatus.DRAFT


@pytest.mark.asyncio
async def test_deployment_snapshot_reference_remains_after_graph_archive(sqlite_db) -> None:
    index = TemplateReferenceIndex(
        sqlite_db,
        tenant_id="tenant-a",
        workspace_id=None,
    )
    registry = DatabaseTemplateRegistry(
        sqlite_db,
        tenant_id="tenant-a",
        workspace_id=None,
    )
    graphs = GraphRepository(sqlite_db, template_reference_index=index)
    deployments = SQLiteDeploymentRepository(
        sqlite_db,
        template_reference_index=index,
    )
    service = DeploymentService(
        graph_repository=graphs,
        deployment_repository=deployments,
    )
    await registry.register("grounded-answer", 3, "Answer {{ input.question }}")
    draft = _graph_with_template(
        graph_id="deployed-indexed-graph",
        status=GraphStatus.DRAFT,
        template_version=3,
    ).model_copy(update={"tenant_id": "tenant-a", "workspace_id": None})
    draft = draft.model_copy(
        update={
            "nodes": [
                node.model_copy(
                    update={"input_contract_ref": None, "output_contract_ref": None}
                )
                for node in draft.nodes
            ]
        }
    )
    await graphs.create(draft, tenant_id="tenant-a", workspace_id=None)
    await graphs.publish(
        draft.graph_id,
        draft.version,
        tenant_id="tenant-a",
        workspace_id=None,
    )
    await service.deploy(
        "indexed-deployment",
        draft.graph_id,
        draft.version,
        tenant_id="tenant-a",
        workspace_id=None,
    )
    await graphs.archive(
        draft.graph_id,
        draft.version,
        tenant_id="tenant-a",
        workspace_id=None,
    )

    conflict = await index.find_conflict(
        name="grounded-answer",
        version=3,
        is_latest=True,
    )
    assert conflict is not None
    assert conflict.source_kind == "deployment"
    assert conflict.source_ref == "indexed-deployment@1"
