"""Template CRUD REST API.

Provides:
  GET    /templates              -- List all templates
  POST   /templates              -- Register a new template
  GET    /templates/{name}       -- Get latest (or specific version via ?version=N)
  DELETE /templates/{name}/{version} -- Remove a template version
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from zeroth.core.service.authorization import Permission, require_permission
from zeroth.core.templates.errors import TemplateNotFoundError, TemplateVersionExistsError


class CreateTemplateRequest(BaseModel):
    """Request body for creating a new template."""

    model_config = ConfigDict(extra="forbid")

    name: str
    version: int = 1
    template_str: str
    variables: list[str] = []
    description: str = ""


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
        templates = registry.list()
        return TemplateListResponse(
            templates=[
                TemplateResponse(
                    name=t.name,
                    version=t.version,
                    template_str=t.template_str,
                    variables=t.variables,
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
        await require_permission(request, Permission.TEMPLATE_ADMIN)
        registry = _template_registry(request)
        try:
            template = registry.register(
                name=payload.name,
                version=payload.version,
                template_str=payload.template_str,
                variables=payload.variables if payload.variables else None,
            )
        except TemplateVersionExistsError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="template version already exists",
            ) from exc
        return TemplateResponse(
            name=template.name,
            version=template.version,
            template_str=template.template_str,
            variables=template.variables,
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
            template = registry.get(name, version)
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
        await require_permission(request, Permission.TEMPLATE_ADMIN)
        registry = _template_registry(request)
        try:
            registry.delete(name, version)
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
