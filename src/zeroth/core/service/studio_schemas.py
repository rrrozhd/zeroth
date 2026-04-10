"""Pydantic request/response models for the Studio graph authoring API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class StudioPosition(BaseModel):
    """Canvas position for a node in the Studio editor."""

    x: float = 0.0
    y: float = 0.0


class StudioViewport(BaseModel):
    """Viewport state for the Studio canvas."""

    x: float = 0.0
    y: float = 0.0
    zoom: float = 1.0


class StudioNodeResponse(BaseModel):
    """A node as represented in the Studio frontend."""

    id: str
    type: str  # One of 8 frontend visual types
    position: StudioPosition
    data: dict[str, Any] = Field(default_factory=dict)


class StudioEdgeResponse(BaseModel):
    """An edge as represented in the Studio frontend."""

    id: str
    source: str
    target: str
    source_handle: str | None = None
    target_handle: str | None = None


class CreateWorkflowRequest(BaseModel):
    """Request body for creating a new workflow."""

    name: str = Field(min_length=1, max_length=200)


class UpdateWorkflowRequest(BaseModel):
    """Request body for updating an existing workflow."""

    name: str | None = None
    nodes: list[StudioNodeResponse] | None = None
    edges: list[StudioEdgeResponse] | None = None
    viewport: StudioViewport | None = None


class WorkflowSummaryResponse(BaseModel):
    """Compact workflow representation for list views."""

    id: str
    name: str
    version: int
    status: str
    updated_at: str


class WorkflowDetailResponse(BaseModel):
    """Full workflow representation with nodes, edges, and viewport."""

    id: str
    name: str
    version: int
    status: str
    nodes: list[StudioNodeResponse]
    edges: list[StudioEdgeResponse]
    viewport: StudioViewport
    updated_at: str


class PortDefinitionResponse(BaseModel):
    """A port on a node type."""

    id: str
    type: str
    direction: str
    label: str


class NodeTypeResponse(BaseModel):
    """A node type available in the Studio palette."""

    type: str
    label: str
    category: str
    ports: list[PortDefinitionResponse]
