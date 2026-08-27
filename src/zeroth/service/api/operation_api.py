"""Authorized resolution of side effects whose external outcome is unknown."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4

from fastapi import APIRouter, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from zeroth.governance.audit import AuditRepository, NodeAuditRecord
from zeroth.platform.dispatch.operations import OperationState
from zeroth.platform.signing import NullSigner
from zeroth.service.api.authorization import (
    Permission,
    require_deployment_scope,
    require_permission,
)


class OperatorResolution(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


class OperationResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolution: OperatorResolution
    reason: str = Field(min_length=1, max_length=2000)
    receipt: Any | None = None


class OperationResolutionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_key: str
    state: OperationState


class OperationApiBootstrapLike(Protocol):
    deployment: object
    orchestrator: object
    audit_repository: AuditRepository
    signer: object | None


def register_operation_routes(app: FastAPI | APIRouter) -> None:
    """Register the deployment-scoped operator resolution route."""

    @app.post(
        "/deployments/{deployment_ref}/operations/{operation_key}/resolve",
        response_model=OperationResolutionResponse,
    )
    async def resolve_ambiguous_operation(
        request: Request,
        deployment_ref: str,
        operation_key: str,
        payload: OperationResolutionRequest,
    ) -> OperationResolutionResponse:
        bootstrap = _bootstrap(request)
        deployment = bootstrap.deployment
        await require_deployment_scope(request, deployment)
        if getattr(deployment, "deployment_ref", None) != deployment_ref:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="deployment not found",
            )
        principal = await require_permission(request, Permission.OPERATION_RESOLVE)

        audit_repository: AuditRepository = bootstrap.audit_repository
        signer = getattr(bootstrap, "signer", None)
        audit_signer = getattr(audit_repository, "_signer", None)
        if (
            signer is None
            or isinstance(signer, NullSigner)
            or audit_signer is None
            or isinstance(audit_signer, NullSigner)
        ):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="operation resolution requires audit signing",
            )

        store = getattr(bootstrap.orchestrator, "operation_store", None)
        if store is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="side-effect operation store is unavailable",
            )
        existing = await store.get(operation_key)
        if existing is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="operation not found")
        if existing["state"] != OperationState.AMBIGUOUS:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="only an ambiguous operation can be resolved",
            )

        receipt_json = (
            None
            if payload.receipt is None
            else json.dumps(payload.receipt, default=str, sort_keys=True)
        )
        resolved_state = (
            OperationState.COMPLETED
            if payload.resolution is OperatorResolution.COMPLETED
            else OperationState.FAILED
        )
        # Persist the signed authorization before changing the operation. This
        # ordering guarantees that an operation can never become resolved
        # without a durable signed audit record. A losing concurrent request is
        # still honestly represented as an authorized resolution attempt.
        now = datetime.now(UTC)
        audit = await audit_repository.write(
            NodeAuditRecord(
                audit_id=f"operation-resolution:{uuid4().hex}",
                run_id=existing["run_id"],
                node_id="operation.resolve",
                graph_version_ref=getattr(deployment, "graph_version_ref", "service"),
                deployment_ref=getattr(deployment, "deployment_ref", "service"),
                tenant_id=getattr(deployment, "tenant_id", "default"),
                workspace_id=getattr(deployment, "workspace_id", None),
                status="completed",
                cost_usd=0.0,
                estimated_cost_usd=0.0,
                cost_measurement="measured",
                actor=principal.to_actor(),
                execution_metadata={
                    "operation_key": operation_key,
                    "operation_state": resolved_state.value.lower(),
                    "resolution_reason_sha256": hashlib.sha256(
                        payload.reason.encode("utf-8")
                    ).hexdigest(),
                    "receipt_sha256": (
                        None
                        if receipt_json is None
                        else hashlib.sha256(receipt_json.encode("utf-8")).hexdigest()
                    ),
                },
                started_at=now,
                completed_at=now,
            )
        )
        if audit.record_signature is None:
            raise RuntimeError("operation resolution audit was not signed")

        won = await store.resolve_ambiguous(
            operation_key,
            state=resolved_state,
            reason=payload.reason,
            receipt=receipt_json,
        )
        if not won:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="operation was resolved concurrently",
            )
        return OperationResolutionResponse(operation_key=operation_key, state=resolved_state)


def _bootstrap(request: Request) -> OperationApiBootstrapLike:
    bootstrap = getattr(request.app.state, "bootstrap", None)
    if bootstrap is None:
        raise RuntimeError("service bootstrap is not configured")
    return bootstrap
