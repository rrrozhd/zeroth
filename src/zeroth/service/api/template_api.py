"""Template CRUD REST API.

Provides:
  GET    /templates              -- List all templates
  POST   /templates              -- Register a new template
  GET    /templates/{name}       -- Get latest (or specific version via ?version=N)
  DELETE /templates/{name}/{version} -- Remove a template version
"""

from __future__ import annotations

import inspect
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from zeroth.contracts.templates.errors import (
    TemplateNotFoundError,
    TemplateSyntaxValidationError,
    TemplateVersionExistsError,
)
from zeroth.service.api.authorization import Permission, require_permission
from zeroth.service.service_audit import ServiceAuditRecorder


class CreateTemplateRequest(BaseModel):
    """Request body for creating a new template."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    version: int = Field(default=1, ge=1, le=1_000_000)
    template_str: str = Field(min_length=1, max_length=100_000)
    variables: list[str] = Field(default_factory=list, max_length=256)
    description: str = Field(default="", max_length=2_000)

    @field_validator("variables")
    @classmethod
    def _valid_variables(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("template variables must be unique")
        for value in values:
            if not value or len(value) > 128 or not value.replace("_", "a").isalnum():
                raise ValueError("template variables must be identifiers")
        return values


class TemplateResponse(BaseModel):
    """Response for a single template."""

    name: str
    version: int
    template_str: str
    variables: list[str]
    description: str = ""


class TemplateListResponse(BaseModel):
    """Response for listing templates."""

    templates: list[TemplateResponse]


def register_template_routes(app: FastAPI | APIRouter) -> None:
    """Register template CRUD routes."""

    @app.get("/templates", response_model=TemplateListResponse)
    async def list_templates(request: Request) -> TemplateListResponse:
        await require_permission(request, Permission.RUN_READ)
        registry = _template_registry(request)
        templates = await _maybe_await(registry.list())
        return TemplateListResponse(
            templates=[
                TemplateResponse(
                    name=t.name,
                    version=t.version,
                    template_str=t.template_str,
                    variables=t.variables,
                    description=str(t.metadata.get("description", "")),
                )
                for t in templates
            ]
        )

    @app.post(
        "/templates",
        response_model=TemplateResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_template(
        request: Request,
        payload: CreateTemplateRequest,
    ) -> TemplateResponse:
        principal = await require_permission(request, Permission.TEMPLATE_ADMIN)
        registry = _template_registry(request)
        audit = _template_audit_recorder(request)
        _require_template_audit_signing(audit)
        try:
            if _supports_atomic_mutation(registry):
                async with registry.mutation_transaction() as transaction:
                    template = await registry.register_in_transaction(
                        transaction,
                        name=payload.name,
                        version=payload.version,
                        template_str=payload.template_str,
                        variables=payload.variables if payload.variables else None,
                        metadata={"description": payload.description},
                    )
                    await audit.record_template_event(
                        actor=principal.to_actor(),
                        template_name=template.name,
                        template_version=template.version,
                        transition="created",
                        transaction=transaction,
                    )
            else:
                template = await _maybe_await(
                    registry.register(
                        name=payload.name,
                        version=payload.version,
                        template_str=payload.template_str,
                        variables=payload.variables if payload.variables else None,
                        metadata={"description": payload.description},
                    )
                )
                await audit.record_template_event(
                    actor=principal.to_actor(),
                    template_name=template.name,
                    template_version=template.version,
                    transition="created",
                )
        except TemplateVersionExistsError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="template version already exists",
            ) from exc
        except TemplateSyntaxValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="template syntax is invalid",
            ) from exc
        return TemplateResponse(
            name=template.name,
            version=template.version,
            template_str=template.template_str,
            variables=template.variables,
            description=str(template.metadata.get("description", "")),
        )

    @app.get("/templates/{name}", response_model=TemplateResponse)
    async def get_template(
        request: Request,
        name: str,
        version: int | None = None,
    ) -> TemplateResponse:
        await require_permission(request, Permission.RUN_READ)
        registry = _template_registry(request)
        try:
            template = await _maybe_await(registry.get(name, version))
        except TemplateNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="template not found",
            ) from exc
        return TemplateResponse(
            name=template.name,
            version=template.version,
            template_str=template.template_str,
            variables=template.variables,
            description=str(template.metadata.get("description", "")),
        )

    @app.delete(
        "/templates/{name}/{version}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_template(
        request: Request,
        name: str,
        version: int,
    ) -> None:
        principal = await require_permission(request, Permission.TEMPLATE_ADMIN)
        registry = _template_registry(request)
        audit = _template_audit_recorder(request)
        _require_template_audit_signing(audit)
        checker = _template_dependency_checker(request)
        try:
            if _supports_atomic_mutation(registry):
                async with registry.mutation_transaction() as transaction:
                    template = await registry.get_in_transaction(
                        transaction, name, version
                    )
                    latest = await registry.get_in_transaction(transaction, name)
                    conflict = await checker.find_conflict(
                        name=name,
                        version=version,
                        is_latest=latest.version == version,
                        tenant_id=principal.tenant_id,
                        workspace_id=principal.workspace_id,
                        transaction=transaction,
                    )
                    _raise_dependency_conflict(name, version, conflict)
                    await registry.delete_in_transaction(transaction, name, version)
                    await audit.record_template_event(
                        actor=principal.to_actor(),
                        template_name=template.name,
                        template_version=template.version,
                        transition="deleted",
                        transaction=transaction,
                    )
            else:
                template = await _maybe_await(registry.get(name, version))
                latest = await _maybe_await(registry.get(name))
                conflict = await checker.find_conflict(
                    name=name,
                    version=version,
                    is_latest=latest.version == version,
                    tenant_id=principal.tenant_id,
                    workspace_id=principal.workspace_id,
                )
                _raise_dependency_conflict(name, version, conflict)
                await _maybe_await(registry.delete(template.name, template.version))
                await audit.record_template_event(
                    actor=principal.to_actor(),
                    template_name=template.name,
                    template_version=template.version,
                    transition="deleted",
                )
        except TemplateNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="template version not found",
            ) from exc


def _template_registry(request: Request) -> Any:
    """Extract the TemplateRegistry from the bootstrap."""
    bootstrap = getattr(request.app.state, "bootstrap", None)
    if bootstrap is None:
        raise RuntimeError("service bootstrap is not configured")
    template_registry = getattr(bootstrap, "template_registry", None)
    if template_registry is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="template registry not configured",
        )
    return template_registry


def _template_dependency_checker(request: Request) -> Any:
    bootstrap = getattr(request.app.state, "bootstrap", None)
    checker = getattr(bootstrap, "template_dependency_checker", None)
    if checker is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="template dependency checker not configured",
        )
    return checker


def _template_audit_recorder(request: Request) -> ServiceAuditRecorder:
    bootstrap = getattr(request.app.state, "bootstrap", None)
    audit_repository = getattr(bootstrap, "audit_repository", None)
    deployment = getattr(bootstrap, "deployment", None)
    if audit_repository is None or deployment is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="template audit not configured",
        )
    return ServiceAuditRecorder(
        repository=audit_repository,
        deployment=deployment,
        require_signed=True,
    )


def _require_template_audit_signing(audit: ServiceAuditRecorder) -> None:
    try:
        audit.ensure_signing_available()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="signed template audit is unavailable",
        ) from exc


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _supports_atomic_mutation(registry: Any) -> bool:
    return all(
        callable(getattr(registry, name, None))
        for name in (
            "mutation_transaction",
            "register_in_transaction",
            "get_in_transaction",
            "delete_in_transaction",
        )
    )


def _raise_dependency_conflict(name: str, version: int, conflict: Any) -> None:
    if conflict is None:
        return
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=(
            f"template {name}@{version} is referenced by "
            f"{conflict.source_kind} {conflict.source_ref} and cannot be deleted"
        ),
    )
