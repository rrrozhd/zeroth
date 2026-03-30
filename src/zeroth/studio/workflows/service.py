"""Workflow authoring services built on top of GraphRepository."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from zeroth.graph.models import Graph
from zeroth.graph.repository import GraphRepository
from zeroth.studio.leases.repository import WorkflowLeaseRepository
from zeroth.studio.models import WorkflowDetail, WorkflowDraftHead, WorkflowRecord, WorkflowSummary
from zeroth.studio.workflows.repository import WorkflowRepository


class WorkflowNotFoundError(LookupError):
    """Raised when a workflow is missing from the caller's scope."""


class WorkflowLeaseRequiredError(ValueError):
    """Raised when a draft update is attempted without the active lease token."""


class WorkflowRevisionConflictError(ValueError):
    """Raised when a draft update uses a stale revision token."""


class WorkflowService:
    """Create, list, and load workspace-scoped Studio workflows."""

    def __init__(
        self,
        *,
        workflow_repository: WorkflowRepository,
        graph_repository: GraphRepository,
    ) -> None:
        self.workflow_repository = workflow_repository
        self.graph_repository = graph_repository
        # Ensure the lease table exists alongside workflow metadata during bootstrap.
        self.lease_repository = WorkflowLeaseRepository(workflow_repository.database)

    def list_workflows(self, tenant_id: str, workspace_id: str) -> list[WorkflowSummary]:
        """Return workflow summaries only from the requested scope."""
        return self.workflow_repository.list_workflows(tenant_id, workspace_id)

    def create_workflow(
        self,
        tenant_id: str,
        workspace_id: str,
        name: str,
        folder_path: str = "/",
    ) -> WorkflowDetail:
        """Create a workflow record and an empty mutable draft graph."""
        created_at = datetime.now(UTC)
        workflow_id = uuid4().hex
        graph = self.graph_repository.create(
            Graph(
                graph_id=uuid4().hex,
                name=name,
                created_at=created_at,
                updated_at=created_at,
            )
        )
        revision_token = graph.updated_at.isoformat()
        record = WorkflowRecord(
            workflow_id=workflow_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            graph_id=graph.graph_id,
            name=name,
            folder_path=folder_path,
            created_at=created_at,
            updated_at=created_at,
        )
        draft_head = WorkflowDraftHead(
            workflow_id=workflow_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            draft_graph_version=graph.version,
            revision_token=revision_token,
            validation_status="unknown",
            last_saved_at=graph.updated_at,
        )
        self.workflow_repository.create(record, draft_head)
        return self._build_detail(record, draft_head, graph)

    def get_workflow(
        self,
        tenant_id: str,
        workspace_id: str,
        workflow_id: str,
    ) -> WorkflowDetail | None:
        """Load a workflow and its current draft graph only from the owning scope."""
        stored = self.workflow_repository.get_workflow(tenant_id, workspace_id, workflow_id)
        if stored is None:
            return None
        record, draft_head = stored
        graph = self.graph_repository.get(record.graph_id, draft_head.draft_graph_version)
        if graph is None:
            return None
        return self._build_detail(record, draft_head, graph)

    def update_draft(
        self,
        tenant_id: str,
        workspace_id: str,
        workflow_id: str,
        lease_token: str,
        revision_token: str,
        graph: Graph,
    ) -> WorkflowDetail:
        """Persist a full draft graph update behind scope, lease, and revision checks."""
        stored = self.workflow_repository.get_workflow(tenant_id, workspace_id, workflow_id)
        if stored is None:
            raise WorkflowNotFoundError(workflow_id)
        record, draft_head = stored

        lease = self.lease_repository.get_lease(
            workflow_id=workflow_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
        )
        if lease is None or lease.lease_token != lease_token:
            raise WorkflowLeaseRequiredError("lease token required")
        if draft_head.revision_token != revision_token:
            raise WorkflowRevisionConflictError("revision token mismatch")

        saved_at = datetime.now(UTC)
        saved_graph = self.graph_repository.save(
            graph.model_copy(
                update={
                    "graph_id": record.graph_id,
                    "version": draft_head.draft_graph_version,
                    "updated_at": saved_at,
                }
            )
        )
        updated_record = record.model_copy(update={"updated_at": saved_at})
        updated_draft_head = draft_head.model_copy(
            update={
                "revision_token": uuid4().hex,
                "validation_status": "unknown",
                "last_saved_at": saved_at,
            }
        )
        self.workflow_repository.update_draft(
            workflow_id=workflow_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            draft_graph_version=updated_draft_head.draft_graph_version,
            revision_token=updated_draft_head.revision_token,
            validation_status=updated_draft_head.validation_status,
            last_saved_at=updated_draft_head.last_saved_at,
            updated_at=updated_record.updated_at,
        )
        return self._build_detail(updated_record, updated_draft_head, saved_graph)

    def _build_detail(
        self,
        record: WorkflowRecord,
        draft_head: WorkflowDraftHead,
        graph: Graph,
    ) -> WorkflowDetail:
        return WorkflowDetail(
            workflow_id=record.workflow_id,
            tenant_id=record.tenant_id,
            workspace_id=record.workspace_id,
            graph_id=record.graph_id,
            name=record.name,
            folder_path=record.folder_path,
            draft_graph_version=draft_head.draft_graph_version,
            revision_token=draft_head.revision_token,
            validation_status=draft_head.validation_status,
            updated_at=record.updated_at,
            last_saved_at=draft_head.last_saved_at,
            graph=graph,
        )
