from __future__ import annotations

from fastapi.testclient import TestClient

from tests.service.helpers import api_key_headers, scoped_auth_config
from zeroth.identity import ServiceRole
from zeroth.studio.bootstrap import bootstrap_studio, bootstrap_studio_app


def _auth_config():
    return scoped_auth_config(
        ("operator-a", "studio-operator-a", ServiceRole.OPERATOR, "tenant-a", "workspace-a"),
        ("admin-a", "studio-admin-a", ServiceRole.ADMIN, "tenant-a", "workspace-a"),
        ("reviewer-a", "studio-reviewer-a", ServiceRole.REVIEWER, "tenant-a", "workspace-a"),
        ("operator-unscoped", "studio-operator-unscoped", ServiceRole.OPERATOR, "tenant-a", None),
        ("operator-b", "studio-operator-b", ServiceRole.OPERATOR, "tenant-a", "workspace-b"),
        ("admin-tenant-b", "studio-admin-tenant-b", ServiceRole.ADMIN, "tenant-b", "workspace-a"),
    )


def _studio_app(sqlite_db):
    return bootstrap_studio_app(sqlite_db, auth_config=_auth_config())


def test_studio_workflow_routes_require_scoped_operator_or_admin(sqlite_db) -> None:
    app = _studio_app(sqlite_db)

    with TestClient(app) as client:
        anonymous = client.get("/studio/workflows")
        reviewer = client.get(
            "/studio/workflows",
            headers=api_key_headers("studio-reviewer-a"),
        )
        unscoped = client.get(
            "/studio/workflows",
            headers=api_key_headers("studio-operator-unscoped"),
        )
        created = client.post(
            "/studio/workflows",
            json={"name": "Studio Workflow", "folder_path": "/drafts"},
            headers=api_key_headers("studio-operator-a"),
        )
        listing = client.get(
            "/studio/workflows",
            headers=api_key_headers("studio-admin-a"),
        )

    assert anonymous.status_code == 401
    assert anonymous.json() == {"detail": "authentication required"}
    assert reviewer.status_code == 403
    assert unscoped.status_code == 403

    assert created.status_code == 201
    assert created.json() == {
        "workflow_id": created.json()["workflow_id"],
        "workspace_id": "workspace-a",
        "name": "Studio Workflow",
        "folder_path": "/drafts",
        "draft_graph_version": 1,
        "validation_status": "unknown",
    }

    assert listing.status_code == 200
    assert listing.json() == [created.json()]


def test_studio_workflow_detail_enforces_auth_role_scope_and_shape(sqlite_db) -> None:
    bootstrap = bootstrap_studio(sqlite_db, auth_config=_auth_config())
    owned = bootstrap.workflow_service.create_workflow(
        tenant_id="tenant-a",
        workspace_id="workspace-a",
        name="Owned Workflow",
        folder_path="/owned",
    )
    app = _studio_app(sqlite_db)

    with TestClient(app) as client:
        anonymous = client.get(f"/studio/workflows/{owned.workflow_id}")
        reviewer = client.get(
            f"/studio/workflows/{owned.workflow_id}",
            headers=api_key_headers("studio-reviewer-a"),
        )
        unscoped = client.get(
            f"/studio/workflows/{owned.workflow_id}",
            headers=api_key_headers("studio-operator-unscoped"),
        )
        foreign_workspace = client.get(
            f"/studio/workflows/{owned.workflow_id}",
            headers=api_key_headers("studio-operator-b"),
        )
        foreign_tenant = client.get(
            f"/studio/workflows/{owned.workflow_id}",
            headers=api_key_headers("studio-admin-tenant-b"),
        )
        owned_response = client.get(
            f"/studio/workflows/{owned.workflow_id}",
            headers=api_key_headers("studio-admin-a"),
        )

    assert anonymous.status_code == 401
    assert reviewer.status_code == 403
    assert unscoped.status_code == 403
    assert foreign_workspace.status_code == 404
    assert foreign_tenant.status_code == 404

    assert owned_response.status_code == 200
    assert owned_response.json() == {
        "workflow_id": owned.workflow_id,
        "workspace_id": "workspace-a",
        "name": "Owned Workflow",
        "folder_path": "/owned",
        "revision_token": owned.revision_token,
        "graph": owned.graph.model_dump(mode="json"),
    }


def test_studio_lease_routes_enforce_scope_and_surface_conflicts(sqlite_db) -> None:
    bootstrap = bootstrap_studio(sqlite_db, auth_config=_auth_config())
    owned = bootstrap.workflow_service.create_workflow(
        tenant_id="tenant-a",
        workspace_id="workspace-a",
        name="Leased Workflow",
    )
    app = _studio_app(sqlite_db)

    with TestClient(app) as client:
        created = client.post(
            f"/studio/workflows/{owned.workflow_id}/leases",
            headers=api_key_headers("studio-operator-a"),
        )
        conflict = client.post(
            f"/studio/workflows/{owned.workflow_id}/leases",
            headers=api_key_headers("studio-admin-a"),
        )
        renewed = client.post(
            f"/studio/workflows/{owned.workflow_id}/leases/ping",
            json={"lease_token": created.json()["lease_token"]},
            headers=api_key_headers("studio-operator-a"),
        )
        foreign_workspace = client.post(
            f"/studio/workflows/{owned.workflow_id}/leases",
            headers=api_key_headers("studio-operator-b"),
        )
        released = client.delete(
            f"/studio/workflows/{owned.workflow_id}/leases/{created.json()['lease_token']}",
            headers=api_key_headers("studio-operator-a"),
        )

    assert created.status_code == 201
    assert created.json() == {
        "workflow_id": owned.workflow_id,
        "workspace_id": "workspace-a",
        "lease_token": created.json()["lease_token"],
        "expires_at": created.json()["expires_at"],
    }

    assert conflict.status_code == 409
    assert conflict.json() == {
        "workflow_id": owned.workflow_id,
        "workspace_id": "workspace-a",
        "lease_token": created.json()["lease_token"],
        "expires_at": conflict.json()["expires_at"],
    }

    assert renewed.status_code == 200
    assert renewed.json() == {
        "workflow_id": owned.workflow_id,
        "workspace_id": "workspace-a",
        "lease_token": created.json()["lease_token"],
        "expires_at": renewed.json()["expires_at"],
    }

    assert foreign_workspace.status_code == 404
    assert released.status_code == 204
