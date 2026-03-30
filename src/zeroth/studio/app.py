"""FastAPI application factory for the Studio authoring API."""

from __future__ import annotations

from collections.abc import Collection
from typing import Protocol

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from zeroth.identity import AuthenticatedPrincipal, ServiceRole
from zeroth.observability.correlation import (
    get_correlation_id,
    new_correlation_id,
    set_correlation_id,
)
from zeroth.service.auth import AuthenticationError, current_principal, record_service_denial


class StudioBootstrapLike(Protocol):
    """Minimal bootstrap contract needed by the Studio HTTP app."""

    workflow_service: object
    lease_service: object
    graph_repository: object
    contract_registry: object
    auth_config: object
    authenticator: object


def require_studio_principal(
    request: Request,
    *,
    allowed_roles: Collection[ServiceRole] = (ServiceRole.OPERATOR, ServiceRole.ADMIN),
) -> AuthenticatedPrincipal:
    """Require an authenticated Studio principal with workspace scope."""

    principal = current_principal(request)
    bootstrap = getattr(request.app.state, "bootstrap", None)
    if principal.workspace_id is None:
        record_service_denial(
            audit_repository=getattr(bootstrap, "audit_repository", None),
            deployment=None,
            request=request,
            node_id="studio.authorization",
            status="forbidden",
            error="workspace scope required",
            actor=principal.to_actor(),
            metadata={"allowed_roles": [role.value for role in allowed_roles]},
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
    if any(role in allowed_roles for role in principal.roles):
        return principal
    record_service_denial(
        audit_repository=getattr(bootstrap, "audit_repository", None),
        deployment=None,
        request=request,
        node_id="studio.authorization",
        status="forbidden",
        error="missing studio role",
        actor=principal.to_actor(),
        metadata={"allowed_roles": [role.value for role in allowed_roles]},
    )
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")


def create_studio_app(bootstrap: StudioBootstrapLike) -> FastAPI:
    """Create the dedicated Studio authoring API."""

    from zeroth.studio.sessions_api import register_session_routes
    from zeroth.studio.validation_api import register_validation_routes
    from zeroth.studio.workflows_api import register_workflow_routes

    app = FastAPI(title="Zeroth Studio Authoring API")
    app.state.bootstrap = bootstrap

    @app.middleware("http")
    async def authenticate_request(request: Request, call_next):
        cid = request.headers.get("X-Correlation-ID") or new_correlation_id()
        set_correlation_id(cid)
        try:
            request.state.principal = bootstrap.authenticator.authenticate_headers(request.headers)
        except AuthenticationError as exc:
            record_service_denial(
                audit_repository=getattr(bootstrap, "audit_repository", None),
                deployment=None,
                request=request,
                node_id="studio.auth",
                status="unauthenticated",
                error=str(exc),
            )
            return JSONResponse(status_code=401, content={"detail": str(exc)})

        response = await call_next(request)
        response.headers["X-Correlation-ID"] = get_correlation_id()
        return response

    register_workflow_routes(app)
    register_session_routes(app)
    register_validation_routes(app)
    return app
