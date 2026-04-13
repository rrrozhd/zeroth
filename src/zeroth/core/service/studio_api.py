"""Studio graph authoring REST API.

Provides CRUD endpoints for workflows and a node-types registry endpoint.
Visual metadata (positions, viewport) is stored in graph.metadata["studio"]
to avoid modifying core graph models.
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, Response

from zeroth.core.graph import GraphRepository
from zeroth.core.graph.models import Graph, GraphStatus
from zeroth.core.service.studio_schemas import (
    CreateWorkflowRequest,
    NodeTypeResponse,
    PortDefinitionResponse,
    StudioEdgeResponse,
    StudioNodeResponse,
    StudioPosition,
    StudioViewport,
    UpdateWorkflowRequest,
    WorkflowDetailResponse,
    WorkflowSummaryResponse,
)

router = APIRouter(prefix="/api/studio/v1", tags=["studio"])


# ---------------------------------------------------------------------------
# Node type registry (static)
# ---------------------------------------------------------------------------

_NODE_TYPES: list[NodeTypeResponse] = [
    NodeTypeResponse(
        type="start",
        label="Start",
        category="flow",
        ports=[
            PortDefinitionResponse(
                id="output-control", type="control", direction="output", label="Next"
            )
        ],
    ),
    NodeTypeResponse(
        type="end",
        label="End",
        category="flow",
        ports=[
            PortDefinitionResponse(
                id="input-control", type="control", direction="input", label="Done"
            )
        ],
    ),
    NodeTypeResponse(
        type="agent",
        label="Agent",
        category="core",
        ports=[
            PortDefinitionResponse(id="input-data", type="data", direction="input", label="Input"),
            PortDefinitionResponse(
                id="output-data", type="data", direction="output", label="Output"
            ),
        ],
    ),
    NodeTypeResponse(
        type="executionUnit",
        label="Execution Unit",
        category="core",
        ports=[
            PortDefinitionResponse(id="input-data", type="data", direction="input", label="Input"),
            PortDefinitionResponse(
                id="output-data", type="data", direction="output", label="Output"
            ),
        ],
    ),
    NodeTypeResponse(
        type="approvalGate",
        label="Approval Gate",
        category="core",
        ports=[
            PortDefinitionResponse(
                id="input-control", type="control", direction="input", label="Request"
            ),
            PortDefinitionResponse(
                id="output-control", type="control", direction="output", label="Approved"
            ),
        ],
    ),
    NodeTypeResponse(
        type="memoryResource",
        label="Memory Resource",
        category="data",
        ports=[
            PortDefinitionResponse(id="input-data", type="data", direction="input", label="Write"),
            PortDefinitionResponse(id="output-data", type="data", direction="output", label="Read"),
        ],
    ),
    NodeTypeResponse(
        type="conditionBranch",
        label="Condition Branch",
        category="flow",
        ports=[
            PortDefinitionResponse(id="input-data", type="data", direction="input", label="Input"),
            PortDefinitionResponse(id="output-true", type="data", direction="output", label="True"),
            PortDefinitionResponse(
                id="output-false", type="data", direction="output", label="False"
            ),
        ],
    ),
    NodeTypeResponse(
        type="dataMapping",
        label="Data Mapping",
        category="data",
        ports=[
            PortDefinitionResponse(id="input-data", type="data", direction="input", label="Input"),
            PortDefinitionResponse(
                id="output-data", type="data", direction="output", label="Output"
            ),
        ],
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_graph_repository(request: Request) -> GraphRepository:
    """Retrieve the GraphRepository from app state."""
    return request.app.state.bootstrap.graph_repository


def _graph_to_detail(graph: Graph) -> WorkflowDetailResponse:
    """Map a core Graph model to a Studio WorkflowDetailResponse."""
    studio_meta = graph.metadata.get("studio", {})
    node_positions = studio_meta.get("node_positions", {})
    viewport_data = studio_meta.get("viewport", {})

    nodes = []
    for node in graph.nodes:
        pos = node_positions.get(node.node_id, {"x": 0, "y": 0})
        nodes.append(
            StudioNodeResponse(
                id=node.node_id,
                type=node.node_type,
                position=StudioPosition(x=pos.get("x", 0), y=pos.get("y", 0)),
                data={},
            )
        )

    edges = [
        StudioEdgeResponse(
            id=edge.edge_id,
            source=edge.source_node_id,
            target=edge.target_node_id,
        )
        for edge in graph.edges
    ]

    viewport = StudioViewport(
        x=viewport_data.get("x", 0),
        y=viewport_data.get("y", 0),
        zoom=viewport_data.get("zoom", 1),
    )

    return WorkflowDetailResponse(
        id=graph.graph_id,
        name=graph.name,
        version=graph.version,
        status=graph.status.value,
        nodes=nodes,
        edges=edges,
        viewport=viewport,
        updated_at=graph.updated_at.isoformat(),
    )


def _graph_to_summary(graph: Graph) -> WorkflowSummaryResponse:
    """Map a core Graph model to a Studio WorkflowSummaryResponse."""
    return WorkflowSummaryResponse(
        id=graph.graph_id,
        name=graph.name,
        version=graph.version,
        status=graph.status.value,
        updated_at=graph.updated_at.isoformat(),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/workflows", response_model=list[WorkflowSummaryResponse])
async def list_workflows(request: Request) -> list[WorkflowSummaryResponse]:
    """List all workflows as summaries."""
    repo = _get_graph_repository(request)
    graphs = await repo.list()
    # Exclude archived workflows from the list
    return [_graph_to_summary(g) for g in graphs if g.status != GraphStatus.ARCHIVED]


@router.post(
    "/workflows",
    response_model=WorkflowDetailResponse,
    status_code=201,
)
async def create_workflow(
    body: CreateWorkflowRequest,
    request: Request,
) -> WorkflowDetailResponse:
    """Create a new workflow with default Studio metadata."""
    repo = _get_graph_repository(request)
    graph_id = str(uuid4())
    graph = Graph(
        graph_id=graph_id,
        name=body.name,
        nodes=[],
        edges=[],
        metadata={
            "studio": {
                "viewport": {"x": 0, "y": 0, "zoom": 1},
                "node_positions": {},
            }
        },
    )
    saved = await repo.save(graph)
    return _graph_to_detail(saved)


@router.get("/workflows/{workflow_id}", response_model=WorkflowDetailResponse)
async def get_workflow(workflow_id: str, request: Request) -> WorkflowDetailResponse:
    """Get a workflow with full detail including nodes, edges, and viewport."""
    repo = _get_graph_repository(request)
    graph = await repo.get(workflow_id)
    if graph is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return _graph_to_detail(graph)


@router.put("/workflows/{workflow_id}", response_model=WorkflowDetailResponse)
async def update_workflow(
    workflow_id: str,
    body: UpdateWorkflowRequest,
    request: Request,
) -> WorkflowDetailResponse:
    """Update a workflow's name, nodes, edges, or viewport."""
    repo = _get_graph_repository(request)
    graph = await repo.get(workflow_id)
    if graph is None:
        raise HTTPException(status_code=404, detail="Workflow not found")

    updates: dict = {}

    if body.name is not None:
        updates["name"] = body.name

    # Update studio metadata if visual properties are provided
    studio_meta = dict(graph.metadata.get("studio", {}))
    if body.viewport is not None:
        studio_meta["viewport"] = body.viewport.model_dump()
    if body.nodes is not None:
        positions = {}
        for node in body.nodes:
            positions[node.id] = node.position.model_dump()
        studio_meta["node_positions"] = positions
    if body.viewport is not None or body.nodes is not None:
        metadata = dict(graph.metadata)
        metadata["studio"] = studio_meta
        updates["metadata"] = metadata

    if updates:
        updated_graph = graph.model_copy(update=updates)
        saved = await repo.save(updated_graph)
    else:
        saved = graph

    return _graph_to_detail(saved)


@router.delete("/workflows/{workflow_id}", status_code=204)
async def delete_workflow(workflow_id: str, request: Request) -> Response:
    """Archive a workflow (soft delete)."""
    repo = _get_graph_repository(request)
    graph = await repo.get(workflow_id)
    if graph is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    archived = graph.archive()
    await repo.save(archived)
    return Response(status_code=204)


@router.get("/node-types", response_model=list[NodeTypeResponse])
def list_node_types() -> list[NodeTypeResponse]:
    """Return all available node types with their port definitions."""
    return _NODE_TYPES
