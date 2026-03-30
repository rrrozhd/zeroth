"""HTTP routes for Studio workflow authoring."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from zeroth.graph.models import Graph
from zeroth.studio.app import require_studio_principal
from zeroth.studio.workflows.service import (
    WorkflowLeaseRequiredError,
    WorkflowNotFoundError,
    WorkflowRevisionConflictError,
)


class WorkflowSummaryResponse(BaseModel):
    """Workflow summary returned by Studio list and create routes."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    workspace_id: str
    name: str
    folder_path: str
    draft_graph_version: int
    validation_status: str


class WorkflowCreateRequest(BaseModel):
    """Create request payload for a Studio workflow draft."""

    model_config = ConfigDict(extra="forbid")

    name: str
    folder_path: str = "/"


class WorkflowDetailResponse(BaseModel):
    """Workflow detail returned to Studio authoring clients."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    workspace_id: str
    name: str
    folder_path: str
    revision_token: str
    last_saved_at: str
    graph: Graph


class WorkflowDraftUpdateRequest(BaseModel):
    """Full-draft save payload for Studio authoring clients."""

    model_config = ConfigDict(extra="forbid")

    lease_token: str
    revision_token: str
    graph: Graph


def register_workflow_routes(app: FastAPI) -> None:
    """Register the Studio workflow list/create/detail routes."""

    router = APIRouter(tags=["studio-workflows"])

    @router.get("/studio/workflows", response_model=list[WorkflowSummaryResponse])
    async def list_workflows(request: Request) -> list[WorkflowSummaryResponse]:
        principal = require_studio_principal(request)
        workflow_service = request.app.state.bootstrap.workflow_service
        workflows = workflow_service.list_workflows(principal.tenant_id, principal.workspace_id)
        return [
            WorkflowSummaryResponse(
                workflow_id=workflow.workflow_id,
                workspace_id=workflow.workspace_id,
                name=workflow.name,
                folder_path=workflow.folder_path,
                draft_graph_version=workflow.draft_graph_version,
                validation_status=workflow.validation_status,
            )
            for workflow in workflows
        ]

    @router.post(
        "/studio/workflows",
        response_model=WorkflowSummaryResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_workflow(
        payload: WorkflowCreateRequest,
        request: Request,
    ) -> WorkflowSummaryResponse:
        principal = require_studio_principal(request)
        workflow_service = request.app.state.bootstrap.workflow_service
        workflow = workflow_service.create_workflow(
            tenant_id=principal.tenant_id,
            workspace_id=principal.workspace_id,
            name=payload.name,
            folder_path=payload.folder_path,
        )
        return WorkflowSummaryResponse(
            workflow_id=workflow.workflow_id,
            workspace_id=workflow.workspace_id,
            name=workflow.name,
            folder_path=workflow.folder_path,
            draft_graph_version=workflow.draft_graph_version,
            validation_status=workflow.validation_status,
        )

    @router.get(
        "/studio/workflows/{workflow_id}",
        response_model=WorkflowDetailResponse,
    )
    async def get_workflow(workflow_id: str, request: Request) -> WorkflowDetailResponse:
        principal = require_studio_principal(request)
        workflow_service = request.app.state.bootstrap.workflow_service
        workflow = workflow_service.get_workflow(
            tenant_id=principal.tenant_id,
            workspace_id=principal.workspace_id,
            workflow_id=workflow_id,
        )
        if workflow is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="workflow not found")
        return WorkflowDetailResponse(
            workflow_id=workflow.workflow_id,
            workspace_id=workflow.workspace_id,
            name=workflow.name,
            folder_path=workflow.folder_path,
            revision_token=workflow.revision_token,
            last_saved_at=workflow.last_saved_at.isoformat(),
            graph=workflow.graph,
        )

    @router.put(
        "/studio/workflows/{workflow_id}/draft",
        response_model=WorkflowDetailResponse,
    )
    async def update_draft(
        workflow_id: str,
        payload: WorkflowDraftUpdateRequest,
        request: Request,
    ) -> WorkflowDetailResponse:
        principal = require_studio_principal(request)
        workflow_service = request.app.state.bootstrap.workflow_service
        try:
            workflow = workflow_service.update_draft(
                tenant_id=principal.tenant_id,
                workspace_id=principal.workspace_id,
                workflow_id=workflow_id,
                lease_token=payload.lease_token,
                revision_token=payload.revision_token,
                graph=payload.graph,
            )
        except WorkflowNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="workflow not found",
            ) from exc
        except WorkflowLeaseRequiredError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        except WorkflowRevisionConflictError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        return WorkflowDetailResponse(
            workflow_id=workflow.workflow_id,
            workspace_id=workflow.workspace_id,
            name=workflow.name,
            folder_path=workflow.folder_path,
            revision_token=workflow.revision_token,
            last_saved_at=workflow.last_saved_at.isoformat(),
            graph=workflow.graph,
        )

    app.include_router(router)
