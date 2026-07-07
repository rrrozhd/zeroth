"""Memory connector listing REST API.

Surfaces the connector registry populated at bootstrap so operator UIs — the
console's retrieval-node inspector in particular — can offer the actual
resolvable ``connector_ref`` values instead of a blind free-text field.
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, Request
from pydantic import BaseModel, ConfigDict

from zeroth.core.service.authorization import Permission, require_permission


class ConnectorSummaryResponse(BaseModel):
    """One registered memory connector, as shown in the console."""

    model_config = ConfigDict(extra="forbid")

    ref: str
    connector_type: str
    scope: str
    backend: str


def register_connector_routes(app: FastAPI | APIRouter) -> None:
    """Register memory connector listing routes on the FastAPI app."""

    @app.get("/connectors", response_model=list[ConnectorSummaryResponse])
    async def list_connectors(request: Request) -> list[ConnectorSummaryResponse]:
        """All memory connectors registered for this deployment, by ref.

        These are the values a retrieval node's ``connector_ref`` (and an
        agent's ``memory_refs``) can resolve at run time.
        """
        await require_permission(request, Permission.DEPLOYMENT_READ)
        registry = request.app.state.bootstrap.memory_registry
        if registry is None:
            return []
        return sorted(
            (
                ConnectorSummaryResponse(
                    ref=ref,
                    connector_type=manifest.connector_type,
                    scope=str(getattr(manifest.scope, "value", manifest.scope)),
                    backend=type(connector).__name__,
                )
                for ref, (manifest, connector) in registry.list().items()
            ),
            key=lambda c: c.ref,
        )
