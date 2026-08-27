"""Pydantic request/response models for the Studio graph authoring API."""

from __future__ import annotations

import inspect
from typing import Any, Literal

from pydantic import BaseModel, Field

from zeroth.contracts.graph.models import Condition
from zeroth.contracts.mappings.models import EdgeMapping


class StudioPosition(BaseModel):
    """Canvas position for a node in the Studio editor."""

    x: float = 0.0
    y: float = 0.0


class StudioViewport(BaseModel):
    """Viewport state for the Studio canvas."""

    x: float = 0.0
    y: float = 0.0
    zoom: float = 1.0


class StudioExecutionSettings(BaseModel):
    """Authorable graph safety ceilings exposed by Studio.

    Runtime-mode, failure-policy, and audit controls deliberately remain outside
    the canvas contract. Studio authors only the bounds needed to make cycles
    and long-running graphs fail closed.
    """

    max_total_steps: int = Field(default=1000, ge=1)
    max_total_runtime_seconds: int | None = Field(default=None, ge=1)
    max_visits_per_node: int = Field(default=10, ge=1)
    max_visits_per_edge: int | None = Field(default=None, ge=1)
    default_timeout_seconds: int | None = Field(default=None, ge=1)


class StudioNodeResponse(BaseModel):
    """A node as represented in the Studio frontend."""

    id: str
    type: str  # One of 8 frontend visual types
    position: StudioPosition
    data: dict[str, Any] = Field(default_factory=dict)


class StudioEdgeInput(BaseModel):
    """An edge accepted from the Studio frontend.

    ``kind="tool"`` marks a tool attachment (agent → executable unit)
    rather than a control-flow connection.
    """

    id: str
    source: str
    target: str
    source_handle: str | None = None
    target_handle: str | None = None
    kind: Literal["data", "tool"] = "data"
    mapping: EdgeMapping | None = None
    condition: Condition | None = None
    enabled: bool = True


class StudioEdgeResponse(StudioEdgeInput):
    """An edge returned to the Studio frontend."""


class CreateWorkflowRequest(BaseModel):
    """Request body for creating a new workflow."""

    name: str = Field(min_length=1, max_length=200)


class UpdateWorkflowRequest(BaseModel):
    """Request body for updating an existing workflow."""

    name: str | None = None
    nodes: list[StudioNodeResponse] | None = None
    edges: list[StudioEdgeInput] | None = None
    viewport: StudioViewport | None = None
    # Entrypoint node id. Omit to leave unchanged; send "" to clear.
    entry_step: str | None = None
    execution_settings: StudioExecutionSettings | None = None


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
    entry_step: str | None = None
    nodes: list[StudioNodeResponse]
    edges: list[StudioEdgeResponse]
    viewport: StudioViewport
    execution_settings: StudioExecutionSettings = Field(default_factory=StudioExecutionSettings)
    updated_at: str


class StudioContractResponse(BaseModel):
    """A registered contract, for canvas contract-ref pickers."""

    name: str
    version: int
    json_schema: dict = Field(default_factory=dict)


class CreateContractRequest(BaseModel):
    """Request body for registering a schema-only contract from the console."""

    name: str = Field(min_length=1, max_length=200, pattern=r"^\S+$")
    json_schema: dict
    metadata: dict = Field(default_factory=dict)


class PortDefinitionResponse(BaseModel):
    """A port on a node type."""

    id: str
    type: str
    direction: str
    label: str


class NodeTypeResponse(BaseModel):
    """A node type the Studio knows how to draw.

    Knowing how to *draw* a type and letting an author *create* one are separate
    questions. An imported MCP tool has to appear here so the canvas can resolve
    its ports -- without an entry it would render with no handles and its tool
    edge would silently fail to attach, showing an agent and its tool as
    disconnected -- but it must not appear in the palette.
    """

    type: str
    label: str
    category: str
    ports: list[PortDefinitionResponse]


_studio_edge_parameters = inspect.signature(StudioEdgeResponse).parameters
StudioEdgeResponse.__signature__ = inspect.signature(StudioEdgeResponse).replace(
    parameters=[
        parameter
        for name, parameter in _studio_edge_parameters.items()
        if name not in {"condition", "enabled", "mapping"}
    ]
)

UpdateWorkflowRequest.__signature__ = inspect.signature(UpdateWorkflowRequest).replace(
    parameters=[
        parameter
        for name, parameter in inspect.signature(UpdateWorkflowRequest).parameters.items()
        if name != "execution_settings"
    ]
)
WorkflowDetailResponse.__signature__ = inspect.signature(WorkflowDetailResponse).replace(
    parameters=[
        parameter
        for name, parameter in inspect.signature(WorkflowDetailResponse).parameters.items()
        if name != "execution_settings"
    ]
)
