"""Artifact retrieval REST API.

Provides:
  GET /artifacts/{artifact_id}  -- Retrieve artifact data by key
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request, status
from fastapi.responses import Response

from zeroth.core.artifacts.errors import ArtifactNotFoundError
from zeroth.core.service.authorization import Permission, require_permission


def register_artifact_routes(app: FastAPI | APIRouter) -> None:
    """Register artifact retrieval routes."""

    @app.get("/artifacts/{artifact_id}")
    async def get_artifact(request: Request, artifact_id: str) -> Response:
        await require_permission(request, Permission.RUN_READ)
        store = _artifact_store(request)
        try:
            data = await store.retrieve(artifact_id)
        except ArtifactNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="artifact not found",
            ) from exc
        return Response(content=data, media_type="application/octet-stream")


def _artifact_store(request: Request) -> Any:
    """Extract the ArtifactStore from the bootstrap."""
    bootstrap = getattr(request.app.state, "bootstrap", None)
    if bootstrap is None:
        raise RuntimeError("service bootstrap is not configured")
    artifact_store = getattr(bootstrap, "artifact_store", None)
    if artifact_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="artifact store not configured",
        )
    return artifact_store
