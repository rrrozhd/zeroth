"""Validation and contract lookup routes for Studio authoring."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from zeroth.contracts import ContractNotFoundError, ContractReference
from zeroth.graph.validation import GraphValidator
from zeroth.graph.validation_errors import GraphValidationReport
from zeroth.studio.app import require_studio_principal


class ContractLookupResponse(BaseModel):
    """Serialized contract schema payload for node-local authoring flows."""

    model_config = ConfigDict(extra="forbid")

    name: str
    version: int
    json_schema: dict


def register_validation_routes(app: FastAPI) -> None:
    """Register Studio validation and slash-safe contract lookup routes."""

    router = APIRouter(tags=["studio-validation"])

    @router.post(
        "/studio/workflows/{workflow_id}/validate",
        response_model=GraphValidationReport,
    )
    async def validate_workflow(workflow_id: str, request: Request) -> GraphValidationReport:
        principal = require_studio_principal(request)
        workflow_service = request.app.state.bootstrap.workflow_service
        workflow = workflow_service.get_workflow(
            tenant_id=principal.tenant_id,
            workspace_id=principal.workspace_id,
            workflow_id=workflow_id,
        )
        if workflow is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="workflow not found")
        return GraphValidator().validate(workflow.graph)

    @router.get(
        "/studio/contracts/{contract_ref:path}",
        response_model=ContractLookupResponse,
    )
    async def get_contract(contract_ref: str, request: Request) -> ContractLookupResponse:
        require_studio_principal(request)
        contract_registry = request.app.state.bootstrap.contract_registry
        try:
            contract = contract_registry.resolve(
                ContractReference(name=contract_ref, version=None)
            )
        except ContractNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="contract not found") from exc
        return ContractLookupResponse(
            name=contract.name,
            version=contract.version,
            json_schema=contract.json_schema,
        )

    app.include_router(router)
