"""Tests for the GET /v1/manifests listing endpoint."""

from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient
from pydantic import BaseModel

from tests.service.helpers import agent_graph, deploy_service, operator_headers, reviewer_headers
from zeroth.governance.audit.models import NodeAuditRecord
from zeroth.integrations.execution.models import (
    InputMode,
    NativeUnitManifest,
    OutputMode,
    PythonModuleArtifactSource,
)
from zeroth.integrations.execution.runner import ExecutableUnitBinding
from zeroth.service.bootstrap import bootstrap_app

DEPLOYMENT = "manifests-test"


class _DemoPayload(BaseModel):
    name: str


def _native_binding(manifest_ref: str = "eu://native-unit") -> ExecutableUnitBinding:
    manifest = NativeUnitManifest(
        unit_id="native-unit",
        artifact_source=PythonModuleArtifactSource(ref="demo.native:handler"),
        callable_ref="demo.native:handler",
        entrypoint_type="python_callable",
        input_mode=InputMode.JSON_STDIN,
        output_mode=OutputMode.JSON_STDOUT,
        input_contract_ref="contract://input",
        output_contract_ref="contract://output",
    )
    return ExecutableUnitBinding(
        manifest_ref=manifest_ref,
        manifest=manifest,
        input_model=_DemoPayload,
        output_model=_DemoPayload,
        python_handler=lambda _ctx, data: {"name": data.name},
    )


async def test_list_manifests_returns_units_and_runners(sqlite_db) -> None:
    service, _ = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-manifests"),
        deployment_ref=DEPLOYMENT,
    )
    service.orchestrator.executable_unit_runner.registry.register(_native_binding())
    service.orchestrator.agent_runners["agent-step"] = object()
    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service

    with TestClient(app) as client:
        r = client.get("/v1/manifests", headers=operator_headers())

    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    by_ref = {(m["kind"], m["manifest_ref"]): m for m in body}
    unit = by_ref[("executable_unit", "eu://native-unit")]
    assert unit["runtime"] == "python"
    assert unit["description"] is None
    runner = by_ref[("agent_runner", "agent-step")]
    assert runner["runtime"] is None
    # Sorted by (kind, manifest_ref): agent runners sort before executable units.
    assert body == sorted(body, key=lambda m: (m["kind"], m["manifest_ref"]))


async def test_list_manifests_requires_auth(sqlite_db) -> None:
    service, _ = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-manifests-auth"),
        deployment_ref=DEPLOYMENT + "-auth",
    )
    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service

    with TestClient(app) as client:
        r = client.get("/v1/manifests")

    assert r.status_code == 401


async def test_manifest_detail_exposes_safe_operator_metadata(sqlite_db) -> None:
    service, _ = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-manifest-detail"),
        deployment_ref=DEPLOYMENT + "-detail",
    )
    service.orchestrator.executable_unit_runner.registry.register(_native_binding())
    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service

    with TestClient(app) as client:
        response = client.get(
            "/v1/manifests/eu%3A%2F%2Fnative-unit",
            headers=operator_headers(),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["manifest_ref"] == "eu://native-unit"
    assert body["kind"] == "executable_unit"
    assert body["runtime"] == "python"
    assert body["version"] == 1
    assert body["input_contract_ref"] == "contract://input"
    assert body["output_contract_ref"] == "contract://output"
    assert body["input_schema"]["properties"]["name"]["type"] == "string"
    assert body["output_schema"]["properties"]["name"]["type"] == "string"
    serialized = response.text
    assert "environment_variables" not in serialized
    assert "run_config" not in serialized
    assert "callable_ref" not in serialized


async def test_manifest_run_linkage_is_audit_scoped(sqlite_db) -> None:
    service, _ = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-manifest-runs"),
        deployment_ref=DEPLOYMENT + "-runs",
    )
    service.orchestrator.executable_unit_runner.registry.register(_native_binding())
    await service.audit_repository.write(
        NodeAuditRecord(
            audit_id="audit-manifest-run",
            run_id="run-manifest-linked",
            node_id="native-node",
            graph_version_ref=service.deployment.graph_version_ref,
            deployment_ref=service.deployment.deployment_ref,
            tenant_id=service.deployment.tenant_id,
            workspace_id=service.deployment.workspace_id,
            status="completed",
            execution_metadata={
                "manifest_ref_sha256": hashlib.sha256(b"eu://native-unit").hexdigest()
            },
        )
    )
    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service

    with TestClient(app) as client:
        denied = client.get(
            "/v1/manifests/eu%3A%2F%2Fnative-unit/runs",
            headers=operator_headers(),
        )
        allowed = client.get(
            "/v1/manifests/eu%3A%2F%2Fnative-unit/runs",
            headers=reviewer_headers(),
        )

    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert allowed.json() == {
        "manifest_ref": "eu://native-unit",
        "runs": [
            {
                "run_id": "run-manifest-linked",
                "node_id": "native-node",
                "status": "completed",
            }
        ],
    }


async def test_manifest_detail_returns_not_found_for_unknown_ref(sqlite_db) -> None:
    service, _ = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-manifest-missing"),
        deployment_ref=DEPLOYMENT + "-missing",
    )
    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service

    with TestClient(app) as client:
        response = client.get(
            "/v1/manifests/eu%3A%2F%2Fmissing",
            headers=operator_headers(),
        )

    assert response.status_code == 404
