from __future__ import annotations

import time

from zeroth.graph.repository import GraphRepository
from zeroth.studio.leases.service import WorkflowLeaseService
from zeroth.studio.models import WorkflowLease, WorkflowLeaseConflict
from zeroth.studio.workflows.repository import STUDIO_WORKFLOW_SCHEMA_VERSION, WorkflowRepository
from zeroth.studio.workflows.service import WorkflowService


def _table_columns(sqlite_db, table_name: str) -> list[str]:
    with sqlite_db.transaction() as connection:
        rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    return [str(row["name"]) for row in rows]


def test_create_workflow_persists_workspace_metadata_and_draft_head(sqlite_db) -> None:
    service = WorkflowService(
        workflow_repository=WorkflowRepository(sqlite_db),
        graph_repository=GraphRepository(sqlite_db),
    )

    created = service.create_workflow(
        tenant_id="tenant-a",
        workspace_id="workspace-a",
        name="Workflow Alpha",
        folder_path="/studio",
    )

    assert created.tenant_id == "tenant-a"
    assert created.workspace_id == "workspace-a"
    assert created.name == "Workflow Alpha"
    assert created.folder_path == "/studio"
    assert created.draft_graph_version == 1
    assert created.validation_status == "unknown"
    assert created.revision_token
    assert sqlite_db.fetch_schema_version("studio_workflows") == STUDIO_WORKFLOW_SCHEMA_VERSION
    assert _table_columns(sqlite_db, "workflow_records") == [
        "workflow_id",
        "tenant_id",
        "workspace_id",
        "graph_id",
        "name",
        "folder_path",
        "archived_at",
        "created_at",
        "updated_at",
    ]
    assert _table_columns(sqlite_db, "workflow_draft_heads") == [
        "workflow_id",
        "tenant_id",
        "workspace_id",
        "draft_graph_version",
        "revision_token",
        "validation_status",
        "last_saved_at",
    ]
    assert _table_columns(sqlite_db, "workflow_leases") == [
        "workflow_id",
        "tenant_id",
        "workspace_id",
        "lease_token",
        "subject",
        "acquired_at",
        "expires_at",
    ]

    with sqlite_db.transaction() as connection:
        record_row = connection.execute(
            """
            SELECT workflow_id, tenant_id, workspace_id, graph_id, name, folder_path
            FROM workflow_records
            WHERE workflow_id = ?
            """,
            (created.workflow_id,),
        ).fetchone()
        draft_row = connection.execute(
            """
            SELECT workflow_id, tenant_id, workspace_id, draft_graph_version,
                   revision_token, validation_status
            FROM workflow_draft_heads
            WHERE workflow_id = ?
            """,
            (created.workflow_id,),
        ).fetchone()
        graph_row = connection.execute(
            """
            SELECT graph_id, version, status, payload
            FROM graph_versions
            WHERE graph_id = ?
            """,
            (created.graph_id,),
        ).fetchone()

    assert dict(record_row) == {
        "workflow_id": created.workflow_id,
        "tenant_id": "tenant-a",
        "workspace_id": "workspace-a",
        "graph_id": created.graph_id,
        "name": "Workflow Alpha",
        "folder_path": "/studio",
    }
    assert dict(draft_row) == {
        "workflow_id": created.workflow_id,
        "tenant_id": "tenant-a",
        "workspace_id": "workspace-a",
        "draft_graph_version": 1,
        "revision_token": created.revision_token,
        "validation_status": "unknown",
    }
    assert graph_row["graph_id"] == created.graph_id
    assert graph_row["version"] == 1
    assert graph_row["status"] == "draft"
    assert graph_row["payload"]


def test_workflow_scope_filters_list_and_loads_current_draft_graph(sqlite_db) -> None:
    service = WorkflowService(
        workflow_repository=WorkflowRepository(sqlite_db),
        graph_repository=GraphRepository(sqlite_db),
    )

    owned = service.create_workflow(
        tenant_id="tenant-a",
        workspace_id="workspace-a",
        name="Owned Workflow",
    )
    service.create_workflow(
        tenant_id="tenant-a",
        workspace_id="workspace-b",
        name="Other Workspace Workflow",
    )
    service.create_workflow(
        tenant_id="tenant-b",
        workspace_id="workspace-a",
        name="Other Tenant Workflow",
    )

    summaries = service.list_workflows("tenant-a", "workspace-a")
    assert [summary.workflow_id for summary in summaries] == [owned.workflow_id]
    assert service.get_workflow("tenant-a", "workspace-b", owned.workflow_id) is None
    assert service.get_workflow("tenant-b", "workspace-a", owned.workflow_id) is None

    detail = service.get_workflow("tenant-a", "workspace-a", owned.workflow_id)

    assert detail is not None
    assert detail.workflow_id == owned.workflow_id
    assert detail.tenant_id == "tenant-a"
    assert detail.workspace_id == "workspace-a"
    assert detail.graph.graph_id == owned.graph_id
    assert detail.graph.version == detail.draft_graph_version == 1
    assert detail.graph.status.value == "draft"
    assert detail.revision_token


def test_workflow_leases_enforce_scope_token_and_conflict(sqlite_db) -> None:
    workflow_service = WorkflowService(
        workflow_repository=WorkflowRepository(sqlite_db),
        graph_repository=GraphRepository(sqlite_db),
    )
    workflow = workflow_service.create_workflow(
        tenant_id="tenant-a",
        workspace_id="workspace-a",
        name="Leased Workflow",
    )
    lease_service = WorkflowLeaseService(workflow_repository=workflow_service.workflow_repository)

    first = lease_service.acquire_lease(
        workflow_id=workflow.workflow_id,
        tenant_id="tenant-a",
        workspace_id="workspace-a",
        subject="editor-a",
        ttl_seconds=30,
    )

    assert isinstance(first, WorkflowLease)
    conflict = lease_service.acquire_lease(
        workflow_id=workflow.workflow_id,
        tenant_id="tenant-a",
        workspace_id="workspace-a",
        subject="editor-b",
        ttl_seconds=30,
    )
    assert isinstance(conflict, WorkflowLeaseConflict)
    assert conflict.workflow_id == workflow.workflow_id
    assert conflict.subject == "editor-a"
    assert lease_service.renew_lease(
        workflow_id=workflow.workflow_id,
        tenant_id="tenant-a",
        workspace_id="workspace-b",
        lease_token=first.lease_token,
        ttl_seconds=30,
    ) is None
    assert lease_service.renew_lease(
        workflow_id=workflow.workflow_id,
        tenant_id="tenant-a",
        workspace_id="workspace-a",
        lease_token="wrong-token",
        ttl_seconds=30,
    ) is None

    time.sleep(0.01)
    renewed = lease_service.renew_lease(
        workflow_id=workflow.workflow_id,
        tenant_id="tenant-a",
        workspace_id="workspace-a",
        lease_token=first.lease_token,
        ttl_seconds=30,
    )
    assert renewed is not None
    assert renewed.expires_at > first.expires_at
    assert not lease_service.release_lease(
        workflow_id=workflow.workflow_id,
        tenant_id="tenant-a",
        workspace_id="workspace-b",
        lease_token=first.lease_token,
    )
    assert not lease_service.release_lease(
        workflow_id=workflow.workflow_id,
        tenant_id="tenant-a",
        workspace_id="workspace-a",
        lease_token="wrong-token",
    )
    assert lease_service.release_lease(
        workflow_id=workflow.workflow_id,
        tenant_id="tenant-a",
        workspace_id="workspace-a",
        lease_token=first.lease_token,
    )

    reacquired = lease_service.acquire_lease(
        workflow_id=workflow.workflow_id,
        tenant_id="tenant-a",
        workspace_id="workspace-a",
        subject="editor-b",
        ttl_seconds=30,
    )
    assert isinstance(reacquired, WorkflowLease)
    assert reacquired.subject == "editor-b"
