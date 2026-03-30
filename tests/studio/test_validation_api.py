from __future__ import annotations

from fastapi.testclient import TestClient
from pydantic import BaseModel

from tests.service.helpers import api_key_headers, scoped_auth_config
from zeroth.identity import ServiceRole
from zeroth.studio.bootstrap import bootstrap_studio, bootstrap_studio_app


class InputContract(BaseModel):
    value: int


def _auth_config():
    return scoped_auth_config(
        ("operator-a", "studio-operator-a", ServiceRole.OPERATOR, "tenant-a", "workspace-a"),
        ("operator-b", "studio-operator-b", ServiceRole.OPERATOR, "tenant-a", "workspace-b"),
    )


def _studio_app(sqlite_db):
    return bootstrap_studio_app(sqlite_db, auth_config=_auth_config())


def test_draft_save_requires_scope_lease_and_current_revision(sqlite_db) -> None:
    bootstrap = bootstrap_studio(sqlite_db, auth_config=_auth_config())
    owned = bootstrap.workflow_service.create_workflow(
        tenant_id="tenant-a",
        workspace_id="workspace-a",
        name="Draft Workflow",
        folder_path="/drafts",
    )
    app = _studio_app(sqlite_db)

    with TestClient(app) as client:
        lease = client.post(
            f"/studio/workflows/{owned.workflow_id}/leases",
            headers=api_key_headers("studio-operator-a"),
        )
        graph_payload = owned.graph.model_copy(update={"name": "Draft Workflow Saved"}).model_dump(
            mode="json"
        )
        saved = client.put(
            f"/studio/workflows/{owned.workflow_id}/draft",
            json={
                "lease_token": lease.json()["lease_token"],
                "revision_token": owned.revision_token,
                "graph": graph_payload,
            },
            headers=api_key_headers("studio-operator-a"),
        )
        stale_revision = client.put(
            f"/studio/workflows/{owned.workflow_id}/draft",
            json={
                "lease_token": lease.json()["lease_token"],
                "revision_token": owned.revision_token,
                "graph": graph_payload,
            },
            headers=api_key_headers("studio-operator-a"),
        )
        missing_lease = client.put(
            f"/studio/workflows/{owned.workflow_id}/draft",
            json={
                "lease_token": "wrong-token",
                "revision_token": saved.json()["revision_token"],
                "graph": graph_payload,
            },
            headers=api_key_headers("studio-operator-a"),
        )
        foreign_workspace = client.put(
            f"/studio/workflows/{owned.workflow_id}/draft",
            json={
                "lease_token": lease.json()["lease_token"],
                "revision_token": saved.json()["revision_token"],
                "graph": graph_payload,
            },
            headers=api_key_headers("studio-operator-b"),
        )
        reloaded = client.get(
            f"/studio/workflows/{owned.workflow_id}",
            headers=api_key_headers("studio-operator-a"),
        )

    assert lease.status_code == 201

    assert saved.status_code == 200
    assert saved.json()["graph"]["name"] == "Draft Workflow Saved"
    assert saved.json()["revision_token"] != owned.revision_token
    assert saved.json()["last_saved_at"] is not None

    assert stale_revision.status_code == 409
    assert stale_revision.json() == {"detail": "revision token mismatch"}

    assert missing_lease.status_code == 409
    assert missing_lease.json() == {"detail": "lease token required"}

    assert foreign_workspace.status_code == 404
    assert foreign_workspace.json() == {"detail": "workflow not found"}

    assert reloaded.status_code == 200
    assert reloaded.json()["graph"]["name"] == "Draft Workflow Saved"
    assert reloaded.json()["revision_token"] == saved.json()["revision_token"]


def test_validation_and_contract_lookup_use_persisted_draft_scope(sqlite_db) -> None:
    bootstrap = bootstrap_studio(sqlite_db, auth_config=_auth_config())
    bootstrap.contract_registry.register(InputContract, name="contract://input")
    owned = bootstrap.workflow_service.create_workflow(
        tenant_id="tenant-a",
        workspace_id="workspace-a",
        name="Validate Workflow",
        folder_path="/drafts",
    )
    app = _studio_app(sqlite_db)

    with TestClient(app) as client:
        validation = client.post(
            f"/studio/workflows/{owned.workflow_id}/validate",
            headers=api_key_headers("studio-operator-a"),
        )
        contract = client.get(
            "/studio/contracts/contract://input",
            headers=api_key_headers("studio-operator-a"),
        )

    assert validation.status_code == 200
    assert validation.json()["graph_id"] == owned.graph.graph_id
    assert validation.json()["issues"] == [
        {
            "severity": "error",
            "code": "empty_graph",
            "message": "graph must contain at least one node",
            "graph_id": owned.graph.graph_id,
            "node_id": None,
            "edge_id": None,
            "path": [],
            "details": {},
        }
    ]

    assert contract.status_code == 200
    assert contract.json() == {
        "name": "contract://input",
        "version": 1,
        "json_schema": contract.json()["json_schema"],
    }
    assert contract.json()["json_schema"]["title"] == "InputContract"
