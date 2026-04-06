"""Deployment-scoped public contract exposure API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from zeroth.contracts import ContractReference
from zeroth.contracts.errors import ContractNotFoundError
from zeroth.deployments import DeploymentStatus
from zeroth.runs import RunFailureState
from zeroth.service.authorization import Permission, require_deployment_scope, require_permission
from zeroth.service.run_api import RunStatusResponse


class ContractApiBootstrapLike(Protocol):
    """Minimal bootstrap contract needed by the contract exposure API."""

    deployment: object
    contract_registry: object


class DeploymentVersionMetadataResponse(BaseModel):
    """Public metadata for one deployed version."""

    model_config = ConfigDict(extra="forbid")

    deployment_ref: str
    deployment_version: int
    graph_id: str
    graph_version: int
    graph_version_ref: str
    entry_input_contract_ref: str | None = None
    entry_input_contract_version: int | None = None
    entry_output_contract_ref: str | None = None
    entry_output_contract_version: int | None = None
    deployment_settings_snapshot: dict[str, Any] = Field(default_factory=dict)
    status: DeploymentStatus
    created_at: datetime
    updated_at: datetime
    audit_ref: str | None = None
    timeline_ref: str | None = None
    evidence_ref: str | None = None
    attestation_ref: str | None = None


class PublicContractSchemaResponse(BaseModel):
    """Public contract payload exposed by the deployment API."""

    model_config = ConfigDict(extra="forbid")

    name: str
    version: int
    json_schema: dict[str, Any] = Field(default_factory=dict)


class DeploymentResultErrorStateSchemaResponse(BaseModel):
    """Combined terminal result and error schema for a deployed version."""

    model_config = ConfigDict(extra="forbid")

    deployment_ref: str
    deployment_version: int
    graph_version_ref: str
    result_contract: PublicContractSchemaResponse
    result_state_schema: dict[str, Any] = Field(default_factory=dict)
    error_state_schema: dict[str, Any] = Field(default_factory=dict)


def register_contract_routes(app: FastAPI) -> None:
    """Register the deployment contract exposure routes on the service app."""

    @app.get(
        "/deployments/{deployment_ref}/input-contract",
        response_model=PublicContractSchemaResponse,
    )
    async def get_input_contract(
        request: Request,
        deployment_ref: str,
    ) -> PublicContractSchemaResponse:
        bootstrap, deployment = await _deployment_context(request, deployment_ref)
        await require_permission(request, Permission.DEPLOYMENT_READ)
        return _serialize_contract(
            await _resolve_contract_version(
                bootstrap,
                deployment.entry_input_contract_ref,
                version=deployment.entry_input_contract_version,
                contract_kind="input",
            )
        )

    @app.get(
        "/deployments/{deployment_ref}/output-contract",
        response_model=PublicContractSchemaResponse,
    )
    async def get_output_contract(
        request: Request,
        deployment_ref: str,
    ) -> PublicContractSchemaResponse:
        bootstrap, deployment = await _deployment_context(request, deployment_ref)
        await require_permission(request, Permission.DEPLOYMENT_READ)
        return _serialize_contract(
            await _resolve_contract_version(
                bootstrap,
                deployment.entry_output_contract_ref,
                version=deployment.entry_output_contract_version,
                contract_kind="output",
            )
        )

    @app.get(
        "/deployments/{deployment_ref}/result-error-state-schema",
        response_model=DeploymentResultErrorStateSchemaResponse,
    )
    async def get_result_error_state_schema(
        request: Request,
        deployment_ref: str,
    ) -> DeploymentResultErrorStateSchemaResponse:
        bootstrap, deployment = await _deployment_context(request, deployment_ref)
        await require_permission(request, Permission.DEPLOYMENT_READ)
        # Reuse the same pinned output contract endpoint logic so schema views stay consistent.
        result_contract = _serialize_contract(
            await _resolve_contract_version(
                bootstrap,
                deployment.entry_output_contract_ref,
                version=deployment.entry_output_contract_version,
                contract_kind="output",
            )
        )
        return DeploymentResultErrorStateSchemaResponse(
            deployment_ref=deployment.deployment_ref,
            deployment_version=deployment.version,
            graph_version_ref=deployment.graph_version_ref,
            result_contract=result_contract,
            result_state_schema=RunStatusResponse.model_json_schema(),
            error_state_schema=RunFailureState.model_json_schema(),
        )

    @app.get(
        "/deployments/{deployment_ref}/metadata",
        response_model=DeploymentVersionMetadataResponse,
    )
    async def get_deployment_metadata(
        request: Request,
        deployment_ref: str,
    ) -> DeploymentVersionMetadataResponse:
        _, deployment = await _deployment_context(request, deployment_ref)
        await require_permission(request, Permission.DEPLOYMENT_READ)
        return serialize_deployment_metadata(deployment)


def _bootstrap(request: Request) -> ContractApiBootstrapLike:
    bootstrap = getattr(request.app.state, "bootstrap", None)
    if bootstrap is None:
        raise RuntimeError("service bootstrap is not configured")
    return bootstrap


async def _deployment_context(
    request: Request,
    deployment_ref: str,
) -> tuple[ContractApiBootstrapLike, object]:
    bootstrap = _bootstrap(request)
    deployment = bootstrap.deployment
    await require_deployment_scope(request, deployment)
    if getattr(deployment, "deployment_ref", None) != deployment_ref:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="deployment not found")
    return bootstrap, deployment


async def _resolve_contract_version(
    bootstrap: ContractApiBootstrapLike,
    contract_ref: str | None,
    *,
    version: int | None,
    contract_kind: str,
) -> object:
    if contract_ref is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"deployment has no {contract_kind} contract",
        )
    if version is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(f"deployment snapshot is missing pinned {contract_kind} contract version"),
        )
    try:
        # Contract lookups are version-pinned so redeploys never drift with registry changes.
        return await bootstrap.contract_registry.resolve(
            ContractReference(name=contract_ref, version=version)
        )
    except ContractNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{contract_kind} contract {contract_ref!r} not found",
        ) from exc
    except Exception as exc:  # pragma: no cover - defensive guard
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to resolve {contract_kind} contract",
        ) from exc


def _serialize_contract(contract: object) -> PublicContractSchemaResponse:
    return PublicContractSchemaResponse(
        name=contract.name,
        version=contract.version,
        json_schema=dict(contract.json_schema),
    )


def serialize_deployment_metadata(deployment: object) -> DeploymentVersionMetadataResponse:
    """Build the public deployment metadata payload used across service routes."""
    return DeploymentVersionMetadataResponse(
        deployment_ref=deployment.deployment_ref,
        deployment_version=deployment.version,
        graph_id=deployment.graph_id,
        graph_version=deployment.graph_version,
        graph_version_ref=deployment.graph_version_ref,
        entry_input_contract_ref=deployment.entry_input_contract_ref,
        entry_input_contract_version=deployment.entry_input_contract_version,
        entry_output_contract_ref=deployment.entry_output_contract_ref,
        entry_output_contract_version=deployment.entry_output_contract_version,
        deployment_settings_snapshot=dict(deployment.deployment_settings_snapshot),
        status=deployment.status,
        created_at=deployment.created_at,
        updated_at=deployment.updated_at,
        audit_ref=f"/deployments/{deployment.deployment_ref}/audits",
        timeline_ref=f"/deployments/{deployment.deployment_ref}/timeline",
        evidence_ref=f"/deployments/{deployment.deployment_ref}/evidence",
        attestation_ref=f"/deployments/{deployment.deployment_ref}/attestation",
    )
