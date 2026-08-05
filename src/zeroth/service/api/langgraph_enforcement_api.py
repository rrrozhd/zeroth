"""Authenticated LangGraph adapter decision and evidence endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from zeroth.core.langgraph_gateway.enforcement import (
    DecisionRequestV1,
    DecisionResponseV1,
    EnforcementBoundaryError,
    HeartbeatV1,
    InventoryRegistrationV1,
    LangGraphEnforcementService,
    RunAttestationV1,
)
from zeroth.core.langgraph_gateway.models import GatewayError, RunCapabilityEvidence
from zeroth.platform.observability.correlation import get_correlation_id
from zeroth.service.api.authorization import (
    Permission,
    require_deployment_scope,
    require_permission,
)


def _service(request: Request, deployment_ref: str) -> LangGraphEnforcementService:
    bootstrap = request.app.state.bootstrap
    service = getattr(bootstrap, "langgraph_enforcement_service", None)
    if (
        not isinstance(service, LangGraphEnforcementService)
        or service.deployment_ref != deployment_ref
    ):
        raise HTTPException(status_code=404, detail="deployment not found")
    return service


async def _authorize(request: Request, deployment_ref: str) -> LangGraphEnforcementService:
    bootstrap = request.app.state.bootstrap
    await require_deployment_scope(request, bootstrap.deployment)
    await require_permission(request, Permission.RUN_CREATE)
    return _service(request, deployment_ref)


def _raise_safe(error: EnforcementBoundaryError) -> None:
    raise HTTPException(
        status_code=error.status_code,
        detail=GatewayError(
            code=error.code,
            correlation_id=get_correlation_id(),
            retryable=error.retryable,
            reason="LangGraph enforcement request rejected",
        ).model_dump(mode="json"),
    ) from None


def register_langgraph_enforcement_routes(router: APIRouter) -> None:
    """Register the deployment-scoped adapter boundary."""

    @router.post(
        "/langgraph/deployments/{deployment_ref}/decisions",
        response_model=DecisionResponseV1,
    )
    async def decide(
        deployment_ref: str, body: DecisionRequestV1, request: Request
    ) -> DecisionResponseV1:
        try:
            return await (await _authorize(request, deployment_ref)).decide(body)
        except EnforcementBoundaryError as error:
            _raise_safe(error)

    @router.post(
        "/langgraph/deployments/{deployment_ref}/inventories",
        status_code=204,
    )
    async def register_inventory(
        deployment_ref: str, body: InventoryRegistrationV1, request: Request
    ) -> None:
        try:
            await (await _authorize(request, deployment_ref)).register_inventory(body)
        except EnforcementBoundaryError as error:
            _raise_safe(error)

    @router.post(
        "/langgraph/deployments/{deployment_ref}/attestations",
        response_model=RunCapabilityEvidence,
    )
    async def attest(
        deployment_ref: str, body: RunAttestationV1, request: Request
    ) -> RunCapabilityEvidence:
        try:
            return await (await _authorize(request, deployment_ref)).attest_run(body)
        except EnforcementBoundaryError as error:
            _raise_safe(error)

    @router.post(
        "/langgraph/deployments/{deployment_ref}/heartbeat",
        status_code=204,
    )
    async def heartbeat(deployment_ref: str, body: HeartbeatV1, request: Request) -> None:
        try:
            await (await _authorize(request, deployment_ref)).heartbeat(body)
        except EnforcementBoundaryError as error:
            _raise_safe(error)


__all__ = ["register_langgraph_enforcement_routes"]
