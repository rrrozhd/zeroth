from __future__ import annotations

import pytest
from pydantic import BaseModel

from tests.graph.test_models import build_graph
from zeroth.contracts import ContractRegistry
from zeroth.deployments import (
    DeploymentError,
    DeploymentService,
    DeploymentStatus,
    SQLiteDeploymentRepository,
)
from zeroth.graph import GraphRepository
from zeroth.graph.serialization import deserialize_graph, serialize_graph


class DeploymentInputContract(BaseModel):
    value: int


class DeploymentOutputContract(BaseModel):
    value: int


async def _build_service(sqlite_db) -> DeploymentService:
    graph_repository = GraphRepository(sqlite_db)
    deployment_repository = SQLiteDeploymentRepository(sqlite_db)
    contract_registry = ContractRegistry(sqlite_db)
    await contract_registry.register(DeploymentInputContract, name="contract://input")
    await contract_registry.register(DeploymentOutputContract, name="contract://output")
    return DeploymentService(
        graph_repository=graph_repository,
        deployment_repository=deployment_repository,
        contract_registry=contract_registry,
    )


def _retarget_graph(graph_id: str):
    graph = build_graph()
    return graph.model_copy(
        update={
            "graph_id": graph_id,
            "name": f"{graph.name} {graph_id}",
            "nodes": [
                node.model_copy(update={"graph_version_ref": f"{graph_id}@{graph.version}"})
                for node in graph.nodes
            ],
        }
    )


async def test_deploy_published_graph_succeeds(sqlite_db) -> None:
    service = await _build_service(sqlite_db)
    graph_repository = service.graph_repository
    graph = await graph_repository.create(build_graph())
    published = await graph_repository.publish(graph.graph_id, graph.version)

    deployed = await service.deploy("graph-1-service", graph.graph_id, graph.version)

    assert deployed.deployment_ref == "graph-1-service"
    assert deployed.version == 1
    assert deployed.graph_id == published.graph_id
    assert deployed.graph_version == published.version
    assert deployed.graph_version_ref == f"{published.graph_id}@{published.version}"
    assert deployed.entry_input_contract_ref == "contract://input"
    assert deployed.entry_input_contract_version == 1
    assert deployed.entry_output_contract_ref == "contract://output"
    assert deployed.entry_output_contract_version == 1
    assert deployed.serialized_graph == serialize_graph(published)
    assert deployed.status is DeploymentStatus.ACTIVE
    assert await service.get("graph-1-service", 1) == deployed
    assert await service.list("graph-1-service") == [deployed]


async def test_deploy_draft_graph_fails(sqlite_db) -> None:
    service = await _build_service(sqlite_db)
    graph_repository = service.graph_repository
    graph = await graph_repository.create(build_graph())

    with pytest.raises(DeploymentError, match="published"):
        await service.deploy("graph-1-service", graph.graph_id, graph.version)


async def test_unversioned_deploy_selects_latest_published_version_when_newer_draft_exists(
    sqlite_db,
) -> None:
    service = await _build_service(sqlite_db)
    graph_repository = service.graph_repository
    original = await graph_repository.create(build_graph())
    await graph_repository.publish(original.graph_id, original.version)

    second = await graph_repository.clone_published_to_draft(original.graph_id, 1)
    await graph_repository.save(second)
    await graph_repository.publish(second.graph_id, second.version)

    newer_draft = await graph_repository.clone_published_to_draft(original.graph_id, 2)
    await graph_repository.save(newer_draft)

    deployed = await service.deploy("graph-1-service", original.graph_id)

    assert deployed.graph_version == 2
    assert deployed.graph_version_ref == "graph-1@2"
    assert deployed.serialized_graph == serialize_graph(
        await graph_repository.get(original.graph_id, 2)
    )


async def test_snapshot_integrity_is_preserved(sqlite_db) -> None:
    service = await _build_service(sqlite_db)
    graph_repository = service.graph_repository
    original = await graph_repository.create(build_graph())
    published_v1 = await graph_repository.publish(original.graph_id, original.version)
    deployed_v1 = await service.deploy("graph-1-service", original.graph_id, 1)

    cloned = await graph_repository.clone_published_to_draft(original.graph_id, 1)
    entry_node = cloned.nodes[0].model_copy(
        update={
            "input_contract_ref": "contract://input.v2",
            "output_contract_ref": "contract://output.v2",
        }
    )
    updated_graph = cloned.model_copy(
        update={
            "nodes": [entry_node, *cloned.nodes[1:]],
            "deployment_settings": {"environment": "prod", "region": "us-east-1"},
        }
    )
    await graph_repository.save(updated_graph)
    await graph_repository.publish(updated_graph.graph_id, updated_graph.version)

    persisted = await service.get("graph-1-service", deployed_v1.version)
    assert persisted is not None

    assert persisted.graph_version == 1
    assert persisted.serialized_graph == serialize_graph(published_v1)
    assert persisted.entry_input_contract_ref == "contract://input"
    assert persisted.entry_input_contract_version == 1
    assert persisted.entry_output_contract_ref == "contract://output"
    assert persisted.entry_output_contract_version == 1
    assert persisted.deployment_settings_snapshot == {"environment": "test"}
    assert deserialize_graph(persisted.serialized_graph) == published_v1


async def test_deploy_rejects_missing_entry_contract_registration(sqlite_db) -> None:
    service = await _build_service(sqlite_db)
    graph_repository = service.graph_repository
    original = await graph_repository.create(build_graph())
    cloned = original.model_copy(
        update={
            "nodes": [
                original.nodes[0].model_copy(update={"input_contract_ref": "contract://missing"}),
                *original.nodes[1:],
            ]
        }
    )
    await graph_repository.save(cloned)
    await graph_repository.publish(cloned.graph_id, cloned.version)

    with pytest.raises(DeploymentError, match="not registered"):
        await service.deploy("graph-1-service", cloned.graph_id, cloned.version)


async def test_rollback_creates_new_deployment_version_for_older_published_graph(sqlite_db) -> None:
    service = await _build_service(sqlite_db)
    graph_repository = service.graph_repository
    original = await graph_repository.create(build_graph())
    await graph_repository.publish(original.graph_id, original.version)
    first = await service.deploy("graph-1-service", original.graph_id, 1)

    cloned = await graph_repository.clone_published_to_draft(original.graph_id, 1)
    updated_graph = cloned.model_copy(
        update={
            "deployment_settings": {"environment": "prod"},
            "metadata": {"owner": "team-b"},
        }
    )
    await graph_repository.save(updated_graph)
    await graph_repository.publish(updated_graph.graph_id, updated_graph.version)
    second = await service.deploy("graph-1-service", original.graph_id, 2)

    rolled_back = await service.rollback("graph-1-service", target_graph_version=1)

    assert first.version == 1
    assert second.version == 2
    assert rolled_back.version == 3
    assert rolled_back.graph_version == 1
    assert rolled_back.graph_version_ref == "graph-1@1"
    assert rolled_back.serialized_graph == first.serialized_graph
    assert rolled_back.status is DeploymentStatus.ACTIVE

    history = await service.list("graph-1-service")
    assert [deployment.version for deployment in history] == [1, 2, 3]
    assert [deployment.status for deployment in history] == [
        DeploymentStatus.SUPERSEDED,
        DeploymentStatus.SUPERSEDED,
        DeploymentStatus.ACTIVE,
    ]


async def test_reusing_existing_deployment_ref_for_different_graph_is_rejected(sqlite_db) -> None:
    service = await _build_service(sqlite_db)
    graph_repository = service.graph_repository
    first_graph = await graph_repository.create(build_graph())
    second_graph = await graph_repository.create(_retarget_graph("graph-2"))
    await graph_repository.publish(first_graph.graph_id, first_graph.version)
    await graph_repository.publish(second_graph.graph_id, second_graph.version)

    await service.deploy("shared-service", first_graph.graph_id, first_graph.version)

    with pytest.raises(DeploymentError, match="deployment_ref"):
        await service.deploy("shared-service", second_graph.graph_id, second_graph.version)


async def test_deploy_retries_when_version_insert_races(sqlite_db, monkeypatch) -> None:
    service = await _build_service(sqlite_db)
    graph_repository = service.graph_repository
    graph = await graph_repository.create(build_graph())
    await graph_repository.publish(graph.graph_id, graph.version)

    versions = iter([1, 2])
    original_create = service.deployment_repository.create
    create_attempts = {"count": 0}

    async def fake_next_version(deployment_ref: str) -> int:
        return next(versions)

    async def flaky_create(deployment):
        create_attempts["count"] += 1
        if create_attempts["count"] == 1:
            raise Exception("UNIQUE constraint failed: idx_deployment_versions_ref_version")
        return await original_create(deployment)

    monkeypatch.setattr(service.deployment_repository, "next_version", fake_next_version)
    monkeypatch.setattr(service.deployment_repository, "create", flaky_create)

    deployed = await service.deploy("graph-1-service", graph.graph_id, graph.version)

    assert deployed.version == 2
    assert create_attempts["count"] == 2
