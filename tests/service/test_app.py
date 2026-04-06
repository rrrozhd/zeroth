from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from tests.graph.test_models import build_graph
from tests.service.helpers import default_service_auth_config, operator_headers
from zeroth.contracts import ContractRegistry
from zeroth.deployments import DeploymentService, SQLiteDeploymentRepository
from zeroth.execution_units import ExecutableUnitRunner
from zeroth.graph import GraphRepository
from zeroth.service.bootstrap import (
    DeploymentBootstrapError,
    bootstrap_app,
    bootstrap_service,
)


class AppInputContract(BaseModel):
    value: int


class AppOutputContract(BaseModel):
    value: int


async def _deploy_test_graph(sqlite_db, deployment_ref: str = "graph-1-service"):
    graph_repository = GraphRepository(sqlite_db)
    contract_registry = ContractRegistry(sqlite_db)
    await contract_registry.register(AppInputContract, name="contract://input")
    await contract_registry.register(AppOutputContract, name="contract://output")
    deployment_service = DeploymentService(
        graph_repository=graph_repository,
        deployment_repository=SQLiteDeploymentRepository(sqlite_db),
        contract_registry=contract_registry,
    )
    graph = await graph_repository.create(build_graph())
    await graph_repository.publish(graph.graph_id, graph.version)
    return await deployment_service.deploy(deployment_ref, graph.graph_id, graph.version)


async def test_bootstrap_service_loads_valid_deployment(sqlite_db) -> None:
    deployment = await _deploy_test_graph(sqlite_db)

    service = await bootstrap_service(sqlite_db, deployment_ref=deployment.deployment_ref)

    assert service.deployment == deployment
    assert service.graph.graph_id == deployment.graph_id
    assert service.graph.version == deployment.graph_version
    assert service.run_repository is service.orchestrator.run_repository
    assert service.audit_repository is service.orchestrator.audit_repository
    assert service.approval_service is service.orchestrator.approval_service
    assert service.contract_registry is not None


async def test_bootstrap_service_accepts_injected_runners(sqlite_db) -> None:
    deployment = await _deploy_test_graph(sqlite_db)
    agent_runner = object()
    executable_unit_runner = ExecutableUnitRunner()

    service = await bootstrap_service(
        sqlite_db,
        deployment_ref=deployment.deployment_ref,
        agent_runners={"agent-step": agent_runner},
        executable_unit_runner=executable_unit_runner,
    )

    assert service.orchestrator.agent_runners["agent-step"] is agent_runner
    assert service.orchestrator.executable_unit_runner is executable_unit_runner


async def test_bootstrap_app_forwards_injected_runners(sqlite_db) -> None:
    deployment = await _deploy_test_graph(sqlite_db)
    agent_runner = object()
    executable_unit_runner = ExecutableUnitRunner()

    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=deployment.deployment_ref,
        agent_runners={"agent-step": agent_runner},
        executable_unit_runner=executable_unit_runner,
        auth_config=default_service_auth_config(),
    )

    assert app.state.bootstrap.orchestrator.agent_runners["agent-step"] is agent_runner
    assert app.state.bootstrap.orchestrator.executable_unit_runner is executable_unit_runner


async def test_bootstrap_service_fails_for_missing_deployment(sqlite_db) -> None:
    with pytest.raises(DeploymentBootstrapError, match="missing-service"):
        await bootstrap_service(sqlite_db, deployment_ref="missing-service")


async def test_bootstrap_service_rejects_mismatched_graph_snapshot(sqlite_db, monkeypatch) -> None:
    deployment = await _deploy_test_graph(sqlite_db)

    original_graph = deployment.graph_id
    broken_graph = build_graph().model_copy(update={"graph_id": "graph-2", "version": 2})

    def fake_deserialize_graph(_serialized_graph: str):
        return broken_graph

    monkeypatch.setattr("zeroth.service.bootstrap.deserialize_graph", fake_deserialize_graph)

    with pytest.raises(DeploymentBootstrapError, match=original_graph):
        await bootstrap_service(sqlite_db, deployment_ref=deployment.deployment_ref)


async def test_health_endpoint_returns_success(sqlite_db) -> None:
    deployment = await _deploy_test_graph(sqlite_db)
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=deployment.deployment_ref,
        auth_config=default_service_auth_config(),
    )

    assert app.state.bootstrap.deployment == deployment

    with TestClient(app) as client:
        response = client.get("/health", headers=operator_headers())

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "deployment_ref": deployment.deployment_ref,
        "deployment_version": deployment.version,
        "graph_version_ref": deployment.graph_version_ref,
    }
