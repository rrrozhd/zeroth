"""Studio graph authoring REST API.

Provides CRUD endpoints for workflows and a node-types registry endpoint.
Visual metadata (positions, viewport) is stored in graph.metadata["studio"]
to avoid modifying core graph models.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import ValidationError

from zeroth.core.graph import GraphRepository
from zeroth.core.graph.models import (
    AgentNode,
    AgentNodeData,
    DisplayMetadata,
    Edge,
    ExecutableUnitNode,
    ExecutableUnitNodeData,
    Graph,
    GraphStatus,
    HumanApprovalNode,
    HumanApprovalNodeData,
    Node,
    RetrievalNode,
    RetrievalNodeData,
    SubgraphNode,
)
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
from zeroth.core.subgraph.models import SubgraphNodeData

router = APIRouter(prefix="/api/studio/v1", tags=["studio"])


# ---------------------------------------------------------------------------
# Node type registry (static)
#
# These mirror the executable graph model exactly (the `node_type` discriminator
# on Node), so a node authored on the canvas maps 1:1 to a real graph node and
# persists/executes. Each renders a single data input + output handle.
# ---------------------------------------------------------------------------


def _io_ports() -> list[PortDefinitionResponse]:
    return [
        PortDefinitionResponse(id="input-data", type="data", direction="input", label="Input"),
        PortDefinitionResponse(id="output-data", type="data", direction="output", label="Output"),
    ]


_NODE_TYPES: list[NodeTypeResponse] = [
    NodeTypeResponse(type="agent", label="Agent", category="core", ports=_io_ports()),
    NodeTypeResponse(
        type="executable_unit", label="Executable Unit", category="core", ports=_io_ports()
    ),
    NodeTypeResponse(
        type="human_approval", label="Human Approval", category="core", ports=_io_ports()
    ),
    NodeTypeResponse(type="retrieval", label="Retrieval", category="core", ports=_io_ports()),
    NodeTypeResponse(type="subgraph", label="Subgraph", category="core", ports=_io_ports()),
]


# Maps the node_type discriminator -> (Node class, data field name, data model).
_NODE_BUILDERS: dict[str, tuple[type[Node], str, type]] = {
    "agent": (AgentNode, "agent", AgentNodeData),
    "executable_unit": (ExecutableUnitNode, "executable_unit", ExecutableUnitNodeData),
    "human_approval": (HumanApprovalNode, "human_approval", HumanApprovalNodeData),
    "retrieval": (RetrievalNode, "retrieval", RetrievalNodeData),
    "subgraph": (SubgraphNode, "subgraph", SubgraphNodeData),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_graph_repository(request: Request) -> GraphRepository:
    """Retrieve the GraphRepository from app state."""
    return request.app.state.bootstrap.graph_repository


def _node_config(node: Node) -> dict[str, Any]:
    """Extract a node's type-specific config as a plain dict for the editor."""
    _, field, _ = _NODE_BUILDERS[node.node_type]
    return getattr(node, field).model_dump(mode="json")


def _node_to_studio_data(node: Node) -> dict[str, Any]:
    """The `data` blob the canvas inspector reads/writes for a node."""
    return {
        "label": node.display.title or node.node_id,
        "config": _node_config(node),
        "input_contract_ref": node.input_contract_ref,
        "output_contract_ref": node.output_contract_ref,
    }


def _build_node(sn: StudioNodeResponse, graph_version_ref: str) -> Node:
    """Construct a real executable Node from a canvas node (draft authoring)."""
    builder = _NODE_BUILDERS.get(sn.type)
    if builder is None:
        raise HTTPException(
            status_code=422,
            detail=f"unknown node type {sn.type!r}; expected one of {sorted(_NODE_BUILDERS)}",
        )
    node_cls, field, data_cls = builder
    data = sn.data or {}
    config = data.get("config") or {}
    label = data.get("label") or sn.id
    try:
        return node_cls(
            node_id=sn.id,
            graph_version_ref=graph_version_ref,
            display=DisplayMetadata(title=label),
            input_contract_ref=data.get("input_contract_ref"),
            output_contract_ref=data.get("output_contract_ref"),
            **{field: data_cls(**config)},
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=422, detail=f"invalid config for node {sn.id!r}: {exc.errors()}"
        ) from exc


def _build_edge(se: StudioEdgeResponse) -> Edge:
    """Construct a graph Edge from a canvas edge, preserving visual handles."""
    metadata: dict[str, Any] = {}
    if se.source_handle is not None:
        metadata["source_handle"] = se.source_handle
    if se.target_handle is not None:
        metadata["target_handle"] = se.target_handle
    return Edge(
        edge_id=se.id,
        source_node_id=se.source,
        target_node_id=se.target,
        metadata=metadata,
    )


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
                data=_node_to_studio_data(node),
            )
        )

    edges = [
        StudioEdgeResponse(
            id=edge.edge_id,
            source=edge.source_node_id,
            target=edge.target_node_id,
            source_handle=edge.metadata.get("source_handle"),
            target_handle=edge.metadata.get("target_handle"),
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
    """Update a workflow's name, structure (nodes/edges), and visual layout.

    Only draft graphs are editable — published versions are immutable; clone one
    to a draft first (POST .../clone). When ``nodes`` are supplied they are built
    into real executable graph nodes (and ``edges`` into graph edges), so the
    canvas authors the actual graph, not just visual metadata.
    """
    repo = _get_graph_repository(request)
    graph = await repo.get(workflow_id)
    if graph is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if graph.status is not GraphStatus.DRAFT:
        raise HTTPException(
            status_code=409,
            detail="Only draft workflows can be edited. Clone this workflow to a draft first.",
        )

    updates: dict = {}

    if body.name is not None:
        updates["name"] = body.name

    # Structural authoring: build real nodes/edges. nodes+edges are set together
    # so the Graph validator sees a consistent set (edges must reference nodes).
    if body.nodes is not None:
        graph_version_ref = f"{graph.graph_id}@{graph.version}"
        updates["nodes"] = [_build_node(n, graph_version_ref) for n in body.nodes]
        updates["edges"] = [_build_edge(e) for e in (body.edges or [])]
    elif body.edges is not None:
        updates["edges"] = [_build_edge(e) for e in body.edges]

    # Visual metadata: positions + viewport live in graph.metadata["studio"].
    studio_meta = dict(graph.metadata.get("studio", {}))
    if body.viewport is not None:
        studio_meta["viewport"] = body.viewport.model_dump()
    if body.nodes is not None:
        studio_meta["node_positions"] = {n.id: n.position.model_dump() for n in body.nodes}
    if body.viewport is not None or body.nodes is not None:
        metadata = dict(graph.metadata)
        metadata["studio"] = studio_meta
        updates["metadata"] = metadata

    if not updates:
        return _graph_to_detail(graph)

    updated_graph = graph.model_copy(update=updates)
    # model_copy skips validators; re-validate so dangling edge refs etc. are a
    # clean 422 here rather than a deserialization error on the next read.
    try:
        Graph.model_validate(updated_graph.model_dump())
    except ValidationError as exc:
        # Only surface error messages — the raw errors() carry the input graph
        # (datetimes) and ctx (exception objects), neither JSON-serializable.
        raise HTTPException(
            status_code=422, detail=[e["msg"] for e in exc.errors()]
        ) from exc
    saved = await repo.save(updated_graph)
    return _graph_to_detail(saved)


@router.post(
    "/workflows/{workflow_id}/clone",
    response_model=WorkflowDetailResponse,
    status_code=201,
)
async def clone_workflow(workflow_id: str, request: Request) -> WorkflowDetailResponse:
    """Clone a published workflow into a new editable draft version."""
    repo = _get_graph_repository(request)
    graph = await repo.get(workflow_id)
    if graph is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if graph.status is not GraphStatus.PUBLISHED:
        raise HTTPException(
            status_code=409,
            detail="Only published workflows can be cloned to a draft.",
        )
    draft = await repo.clone_published_to_draft(workflow_id)
    return _graph_to_detail(draft)


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
