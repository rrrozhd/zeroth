from __future__ import annotations

from fastapi.testclient import TestClient
from pydantic import BaseModel

from tests.graph.test_models import build_graph
from tests.service.helpers import admin_headers, default_service_auth_config
from zeroth.contracts import ContractReference, ContractRegistry
from zeroth.deployments import DeploymentService, SQLiteDeploymentRepository
from zeroth.graph import GraphRepository
from zeroth.runs import RunFailureState
from zeroth.service.bootstrap import bootstrap_app
from zeroth.service.contracts_api import (
    DeploymentResultErrorStateSchemaResponse,
    DeploymentVersionMetadataResponse,
    PublicContractSchemaResponse,
)


class DeployedInputContract(BaseModel):
    value: int
    source: str = "external"


class DeployedOutputContract(BaseModel):
    value: int
    accepted: bool


class DeployedInputContractV2(BaseModel):
    value: int
    source: str
    request_id: str


class DeployedOutputContractV2(BaseModel):
    value: int
    accepted: bool
    request_id: str


def _deploy_contract_service(sqlite_db, *, deployment_ref: str = "contract-api-service"):
    graph_repository = GraphRepository(sqlite_db)
    contract_registry = ContractRegistry(sqlite_db)
    contract_registry.register(DeployedInputContract, name="contract://input")
    contract_registry.register(
        DeployedOutputContract,
        name="contract://output",
    )
    deployment_service = DeploymentService(
        graph_repository=graph_repository,
        deployment_repository=SQLiteDeploymentRepository(sqlite_db),
        contract_registry=contract_registry,
    )
    graph = graph_repository.create(build_graph())
    graph_repository.publish(graph.graph_id, graph.version)
    deployment = deployment_service.deploy(deployment_ref, graph.graph_id, graph.version)
    app = bootstrap_app(
        sqlite_db,
        deployment_ref=deployment.deployment_ref,
        auth_config=default_service_auth_config(),
    )
    return app, app.state.bootstrap, deployment


def test_input_contract_endpoint_returns_deployed_contract_version(sqlite_db) -> None:
    app, service, deployment = _deploy_contract_service(sqlite_db)
    expected = PublicContractSchemaResponse(
        name="contract://input",
        version=1,
        json_schema=service.contract_registry.resolve(
            ContractReference(name="contract://input", version=1)
        ).json_schema,
    )

    with TestClient(app) as client:
        response = client.get(
            f"/deployments/{deployment.deployment_ref}/input-contract",
            headers=admin_headers(),
        )

    assert response.status_code == 200
    assert PublicContractSchemaResponse.model_validate(response.json()) == expected


def test_output_contract_endpoint_returns_deployed_contract_version(sqlite_db) -> None:
    app, service, deployment = _deploy_contract_service(sqlite_db)
    expected = PublicContractSchemaResponse(
        name="contract://output",
        version=1,
        json_schema=service.contract_registry.resolve(
            ContractReference(name="contract://output", version=1)
        ).json_schema,
    )

    with TestClient(app) as client:
        response = client.get(
            f"/deployments/{deployment.deployment_ref}/output-contract",
            headers=admin_headers(),
        )

    assert response.status_code == 200
    assert PublicContractSchemaResponse.model_validate(response.json()) == expected


def test_result_error_state_schema_endpoint_exposes_output_contract_and_error_schema(
    sqlite_db,
) -> None:
    app, service, deployment = _deploy_contract_service(sqlite_db)
    expected_output = PublicContractSchemaResponse(
        name="contract://output",
        version=1,
        json_schema=service.contract_registry.resolve(
            ContractReference(name="contract://output", version=1)
        ).json_schema,
    )

    with TestClient(app) as client:
        response = client.get(
            f"/deployments/{deployment.deployment_ref}/result-error-state-schema",
            headers=admin_headers(),
        )

    assert response.status_code == 200
    payload = response.json()
    parsed = DeploymentResultErrorStateSchemaResponse.model_validate(payload)

    assert parsed.deployment_ref == deployment.deployment_ref
    assert parsed.deployment_version == deployment.version
    assert parsed.graph_version_ref == deployment.graph_version_ref
    assert parsed.result_contract == expected_output
    assert parsed.result_state_schema["type"] == "object"
    assert parsed.result_state_schema["properties"]["status"] == {
        "$ref": "#/$defs/RunPublicStatus"
    }
    assert parsed.result_state_schema["$defs"]["RunPublicStatus"]["enum"] == [
        "queued",
        "running",
        "paused_for_approval",
        "succeeded",
        "failed",
        "terminated_by_policy",
        "terminated_by_loop_guard",
    ]
    assert "approval_paused_state" in parsed.result_state_schema["properties"]
    assert "thread_id" in parsed.result_state_schema["properties"]
    assert "terminal_output" in parsed.result_state_schema["properties"]
    assert parsed.error_state_schema == RunFailureState.model_json_schema()
    assert parsed.model_dump(mode="json") == payload


def test_deployment_metadata_endpoint_returns_version_snapshot(sqlite_db) -> None:
    app, service, deployment = _deploy_contract_service(sqlite_db)
    with TestClient(app) as client:
        response = client.get(
            f"/deployments/{deployment.deployment_ref}/metadata",
            headers=admin_headers(),
        )

    assert response.status_code == 200
    parsed = DeploymentVersionMetadataResponse.model_validate(response.json())

    assert parsed.deployment_ref == deployment.deployment_ref
    assert parsed.deployment_version == deployment.version
    assert parsed.graph_id == deployment.graph_id
    assert parsed.graph_version == deployment.graph_version
    assert parsed.graph_version_ref == deployment.graph_version_ref
    assert parsed.entry_input_contract_ref == "contract://input"
    assert parsed.entry_input_contract_version == 1
    assert parsed.entry_output_contract_ref == "contract://output"
    assert parsed.entry_output_contract_version == 1
    assert parsed.deployment_settings_snapshot == {"environment": "test"}
    assert parsed.model_dump(mode="json") == response.json()


def test_schema_serialization_round_trip(sqlite_db) -> None:
    app, _, deployment = _deploy_contract_service(sqlite_db)

    with TestClient(app) as client:
        response = client.get(
            f"/deployments/{deployment.deployment_ref}/result-error-state-schema",
            headers=admin_headers(),
        )

    parsed = DeploymentResultErrorStateSchemaResponse.model_validate(response.json())
    assert parsed.model_dump(mode="json") == response.json()


def test_contract_endpoints_use_deployed_contract_versions(sqlite_db) -> None:
    app, service, deployment = _deploy_contract_service(sqlite_db)
    service.contract_registry.register(
        DeployedInputContractV2,
        name="contract://input",
        version=2,
    )
    service.contract_registry.register(
        DeployedOutputContractV2,
        name="contract://output",
        version=2,
    )

    with TestClient(app) as client:
        input_response = client.get(
            f"/deployments/{deployment.deployment_ref}/input-contract",
            headers=admin_headers(),
        )
        output_response = client.get(
            f"/deployments/{deployment.deployment_ref}/output-contract",
            headers=admin_headers(),
        )

    assert input_response.status_code == 200
    assert output_response.status_code == 200
    assert input_response.json()["version"] == 1
    assert output_response.json()["version"] == 1
    assert "request_id" not in input_response.json()["json_schema"]["properties"]
    assert "request_id" not in output_response.json()["json_schema"]["properties"]


def test_contract_endpoints_fail_closed_for_legacy_unpinned_deployment(sqlite_db) -> None:
    app, service, deployment = _deploy_contract_service(
        sqlite_db,
        deployment_ref="legacy-contract-api",
    )
    service.contract_registry.register(
        DeployedInputContractV2,
        name="contract://input",
        version=2,
    )
    with sqlite_db.transaction() as connection:
        connection.execute(
            """
            UPDATE deployment_versions
            SET entry_input_contract_version = NULL
            WHERE deployment_id = ?
            """,
            (deployment.deployment_id,),
        )
    app = bootstrap_app(
        sqlite_db,
        deployment_ref=deployment.deployment_ref,
        auth_config=default_service_auth_config(),
    )

    with TestClient(app) as client:
        response = client.get(
            f"/deployments/{deployment.deployment_ref}/input-contract",
            headers=admin_headers(),
        )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "deployment snapshot is missing pinned input contract version"
    }


def test_contract_endpoint_returns_404_for_unknown_deployment(sqlite_db) -> None:
    app, _, _ = _deploy_contract_service(sqlite_db)

    with TestClient(app) as client:
        response = client.get("/deployments/missing/input-contract", headers=admin_headers())

    assert response.status_code == 404
    assert response.json() == {"detail": "deployment not found"}
