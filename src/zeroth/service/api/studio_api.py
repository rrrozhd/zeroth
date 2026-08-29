"""Studio graph authoring REST API.

Provides CRUD endpoints for workflows and a node-types registry endpoint.
Visual metadata (positions, viewport) is stored in graph.metadata["studio"]
to avoid modifying core graph models.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import ValidationError

from zeroth.contracts.graph import GraphRepository
from zeroth.contracts.graph.errors import GraphLifecycleError
from zeroth.contracts.graph.models import (
    AgentNode,
    AgentNodeData,
    DisplayMetadata,
    Edge,
    EntrypointNode,
    EntrypointNodeData,
    ExecutableUnitNode,
    ExecutableUnitNodeData,
    Graph,
    GraphStatus,
    HttpRequestNode,
    HttpRequestNodeData,
    HumanApprovalNode,
    HumanApprovalNodeData,
    IfNode,
    IfNodeData,
    LoopNode,
    LoopNodeData,
    MCPToolNode,
    MCPToolNodeData,
    Node,
    RetrievalNode,
    RetrievalNodeData,
    SubgraphNode,
)
from zeroth.contracts.graph.validation.control_nodes import canonicalize_if_route_edges
from zeroth.contracts.graph.validation_errors import GraphValidationError
from zeroth.contracts.registry import ContractReference, contract_scope_context
from zeroth.runtime.agents.models import ModelParams
from zeroth.runtime.agents.provider import (
    LiteLLMProviderAdapter,
    ProviderRequest,
    run_provider_with_timeout,
)
from zeroth.runtime.graph_validation import GraphValidator
from zeroth.runtime.subgraphs.models import SubgraphNodeData
from zeroth.service.api.authorization import Permission, require_permission
from zeroth.service.api.delivery_types import (
    LiveProviderProbe,
    LiveProviderVerificationRequest,
    LiveProviderVerificationResponse,
    WorkflowPreflightIssue,
    WorkflowPreflightResponse,
)
from zeroth.service.api.studio_schemas import (
    CreateContractRequest,
    CreateWorkflowRequest,
    NodeTypeResponse,
    PortDefinitionResponse,
    StudioContractResponse,
    StudioEdgeInput,
    StudioEdgeResponse,
    StudioExecutionSettings,
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


# Tool attachment ports render on their own edge of the node (bottom for the
# agent's source, top for the unit's target) — a separate set of edges from
# the data flow that leaves through the side handles.
_AGENT_TOOLS_PORT = PortDefinitionResponse(
    id="tools", type="tool", direction="output", label="Tools"
)
_TOOL_TARGET_PORT = PortDefinitionResponse(
    id="tool-input", type="tool", direction="input", label="Tool"
)


_NODE_TYPES: list[NodeTypeResponse] = [
    NodeTypeResponse(
        type="entrypoint",
        label="Entrypoint",
        category="core",
        # Runs enter here — there is nothing upstream, so no input port.
        ports=[
            PortDefinitionResponse(
                id="output-data", type="data", direction="output", label="Output"
            )
        ],
    ),
    NodeTypeResponse(
        type="agent",
        label="Agent",
        category="core",
        ports=[*_io_ports(), _AGENT_TOOLS_PORT],
    ),
    NodeTypeResponse(
        type="code",
        label="Code",
        category="core",
        ports=[*_io_ports(), _TOOL_TARGET_PORT],
    ),
    NodeTypeResponse(
        type="executable_unit",
        label="Executable Unit",
        category="core",
        ports=[*_io_ports(), _TOOL_TARGET_PORT],
    ),
    NodeTypeResponse(
        type="human_approval", label="Human Approval", category="core", ports=_io_ports()
    ),
    NodeTypeResponse(
        type="if",
        label="If",
        category="flow",
        ports=[
            PortDefinitionResponse(
                id="input-data", type="data", direction="input", label="Input"
            ),
            PortDefinitionResponse(id="true", type="data", direction="output", label="True"),
            PortDefinitionResponse(
                id="false", type="data", direction="output", label="False"
            ),
        ],
    ),
    NodeTypeResponse(
        type="loop",
        label="Loop",
        category="flow",
        ports=[
            PortDefinitionResponse(
                id="input-data", type="data", direction="input", label="Input"
            ),
            PortDefinitionResponse(
                id="repeat", type="data", direction="output", label="Repeat"
            ),
            PortDefinitionResponse(id="done", type="data", direction="output", label="Done"),
            PortDefinitionResponse(
                id="limit", type="data", direction="output", label="Limit"
            ),
        ],
    ),
    NodeTypeResponse(
        type="mcp_tool",
        label="MCP Tool",
        category="imported",
        # Attachment target only: an agent reaches it over a tool edge, and no
        # data flows through it, so it carries the tool input port alone.
        ports=[_TOOL_TARGET_PORT],
        # Imported with `zeroth-core mcp-import`, never dragged from the palette:
        # the node is pinned to a schema digest taken from a live server, which
        # canvas authoring has no way to produce.
    ),
    NodeTypeResponse(type="retrieval", label="Retrieval", category="core", ports=_io_ports()),
    NodeTypeResponse(
        type="http_request",
        label="HTTP Request",
        category="core",
        ports=_io_ports(),
    ),
    NodeTypeResponse(type="subgraph", label="Subgraph", category="core", ports=_io_ports()),
]


# Maps the node_type discriminator -> (Node class, data field name, data model).
# "code" is a canvas-level alias: it authors an ExecutableUnitNode whose data
# carries inline_source instead of a manifest_ref, so the whole executable-unit
# machinery (validation, integrity, sandbox, diff, audit) applies unchanged.
_NODE_BUILDERS: dict[str, tuple[type[Node], str, type]] = {
    "agent": (AgentNode, "agent", AgentNodeData),
    "entrypoint": (EntrypointNode, "entrypoint", EntrypointNodeData),
    "code": (ExecutableUnitNode, "executable_unit", ExecutableUnitNodeData),
    "executable_unit": (ExecutableUnitNode, "executable_unit", ExecutableUnitNodeData),
    "human_approval": (HumanApprovalNode, "human_approval", HumanApprovalNodeData),
    "if": (IfNode, "condition", IfNodeData),
    "loop": (LoopNode, "loop", LoopNodeData),
    # Registered so a graph containing an imported MCP tool round-trips through
    # the canvas intact. The console cannot CREATE one this increment (no
    # DEFAULT_CONFIG entry, no palette entry); without the builder here,
    # _node_config would KeyError the moment such a graph is opened.
    "mcp_tool": (MCPToolNode, "mcp_tool", MCPToolNodeData),
    "retrieval": (RetrievalNode, "retrieval", RetrievalNodeData),
    "http_request": (HttpRequestNode, "http_request", HttpRequestNodeData),
    "subgraph": (SubgraphNode, "subgraph", SubgraphNodeData),
}


def _studio_type(node: Node) -> str:
    """Canvas type for a node — code nodes render distinctly from ref-based units."""
    if isinstance(node, ExecutableUnitNode) and node.executable_unit.inline_source is not None:
        return "code"
    return node.node_type


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_graph_repository(request: Request) -> GraphRepository:
    """Retrieve the GraphRepository from app state."""
    return request.app.state.bootstrap.graph_repository


def _node_config(node: Node) -> dict[str, Any]:
    """Extract a node's type-specific config as a plain dict for the editor."""
    _, field, _ = _NODE_BUILDERS[node.node_type]
    config = getattr(node, field).model_dump(mode="json")
    if isinstance(node, IfNode) and not node.condition.routes:
        config.pop("routes", None)
    return config


# NodeBase governance fields the canvas doesn't author (yet): emitted to the
# console for visibility, and preserved server-side on save when the payload
# omits them — a client that doesn't know a field must not be able to wipe it.
_GOVERNANCE_FIELDS = (
    "capability_bindings",
    "policy_bindings",
    "execution_config",
    "audit_config",
    "parallel_config",
    "join_config",
)


def _node_to_studio_data(node: Node) -> dict[str, Any]:
    """The `data` blob the canvas inspector reads/writes for a node."""
    return {
        "label": node.display.title or node.node_id,
        "config": _node_config(node),
        "input_contract_ref": node.input_contract_ref,
        "output_contract_ref": node.output_contract_ref,
        "capability_bindings": list(node.capability_bindings),
        "policy_bindings": list(node.policy_bindings),
        "execution_config": dict(node.execution_config),
        "audit_config": dict(node.audit_config),
        "parallel_config": (
            node.parallel_config.model_dump(mode="json")
            if node.parallel_config is not None
            else None
        ),
        "join_config": (
            node.join_config.model_dump(mode="json") if node.join_config is not None else None
        ),
    }


def _build_node(
    sn: StudioNodeResponse, graph_version_ref: str, existing: Node | None = None
) -> Node:
    """Construct a real executable Node from a canvas node (draft authoring).

    Governance fields follow present-wins semantics: a key present in the
    canvas ``data`` is authoritative (an explicit ``[]`` clears), an absent
    key falls back to ``existing`` — the node this id currently maps to in
    the stored graph — so bindings authored via the API/Python survive a
    canvas save. ``node_version`` and non-title display metadata are never
    client-settable and always carry over from ``existing``.
    """
    builder = _NODE_BUILDERS.get(sn.type)
    if builder is None:
        raise HTTPException(
            status_code=422,
            detail=f"unknown node type {sn.type!r}; expected one of {sorted(_NODE_BUILDERS)}",
        )
    node_cls, field, data_cls = builder
    data = sn.data or {}
    config = dict(data.get("config") or {})
    if sn.type == "code":
        # Inline authoring invariants: the mode is always "inline", and the
        # source key must exist even while empty so a cleared editor still
        # saves as a draft (publish is what requires real code).
        config.setdefault("execution_mode", "inline")
        config.setdefault("inline_source", "")
    label = data.get("label") or sn.id
    governed: dict[str, Any] = {}
    for gov_field in _GOVERNANCE_FIELDS:
        if gov_field in data:
            governed[gov_field] = data[gov_field]
        elif existing is not None:
            governed[gov_field] = getattr(existing, gov_field)
    if existing is not None:
        governed["node_version"] = existing.node_version
        display = existing.display.model_copy(update={"title": label})
    else:
        display = DisplayMetadata(title=label)
    try:
        return node_cls(
            node_id=sn.id,
            graph_version_ref=graph_version_ref,
            display=display,
            input_contract_ref=data.get("input_contract_ref"),
            output_contract_ref=data.get("output_contract_ref"),
            **governed,
            **{field: data_cls(**config)},
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=422, detail=f"invalid config for node {sn.id!r}: {exc.errors()}"
        ) from exc


def _build_edge(se: StudioEdgeInput, existing: Edge | None = None) -> Edge:
    """Construct a graph Edge from a canvas edge, preserving visual handles.

    An edge drawn from the agent's tools handle is a tool attachment even if
    an older client doesn't send ``kind`` — the handle id is the ground truth
    of what the author connected.
    """
    metadata: dict[str, Any] = {}
    if se.source_handle is not None:
        metadata["source_handle"] = se.source_handle
    if se.target_handle is not None:
        metadata["target_handle"] = se.target_handle
    kind = "tool" if (se.kind == "tool" or se.source_handle == "tools") else "data"
    mapping = (
        se.mapping
        if "mapping" in se.model_fields_set
        else existing.mapping
        if existing
        else None
    )
    condition = (
        se.condition
        if "condition" in se.model_fields_set
        else existing.condition
        if existing
        else None
    )
    enabled = (
        se.enabled if "enabled" in se.model_fields_set else existing.enabled if existing else True
    )
    return Edge(
        edge_id=se.id,
        source_node_id=se.source,
        target_node_id=se.target,
        kind=kind,
        mapping=mapping,
        condition=condition,
        enabled=enabled,
        metadata=metadata,
    )


def _auto_layout(graph: Graph) -> dict[str, dict[str, float]]:
    """Left-to-right positions for a graph with no stored Studio layout.

    Graphs deployed outside the Studio (e.g. via the deployment API) carry no
    ``metadata["studio"]["node_positions"]``, so every node would default to
    (0, 0) and stack at the canvas origin. Lay nodes out by BFS depth from the
    entry step (x) with siblings staggered vertically (y) so a freshly deployed
    graph renders as a graph on first open. Stored positions always win — this
    only runs when none are present.
    """
    adjacency: dict[str, list[str]] = defaultdict(list)
    for edge in graph.edges:
        adjacency[edge.source_node_id].append(edge.target_node_id)

    # Seed the BFS from entry_step, or the first node if entry_step is unset —
    # otherwise every node collapses into the depth-0 column.
    seed = graph.entry_step or (graph.nodes[0].node_id if graph.nodes else None)
    depth: dict[str, int] = {}
    if seed is not None:
        depth[seed] = 0
        queue = deque([seed])
        while queue:
            current = queue.popleft()
            for target in adjacency[current]:
                if target not in depth:
                    depth[target] = depth[current] + 1
                    queue.append(target)

    # Nodes unreachable from the seed go in a column past the reached ones
    # rather than colliding with the entry node at depth 0.
    orphan_depth = (max(depth.values()) + 1) if depth else 0
    for node in graph.nodes:
        depth.setdefault(node.node_id, orphan_depth)

    by_depth: dict[int, list[str]] = defaultdict(list)
    for node in graph.nodes:
        by_depth[depth[node.node_id]].append(node.node_id)

    positions: dict[str, dict[str, float]] = {}
    for level, node_ids in by_depth.items():
        for row, node_id in enumerate(node_ids):
            positions[node_id] = {"x": level * 320.0, "y": row * 180.0}
    return positions


def _graph_to_detail(graph: Graph) -> WorkflowDetailResponse:
    """Map a core Graph model to a Studio WorkflowDetailResponse."""
    studio_meta = graph.metadata.get("studio")
    studio_meta = studio_meta if isinstance(studio_meta, dict) else {}
    stored_positions = studio_meta.get("node_positions")
    stored_positions = stored_positions if isinstance(stored_positions, dict) else {}
    viewport_data = studio_meta.get("viewport")
    viewport_data = viewport_data if isinstance(viewport_data, dict) else {}

    # Fill any node missing a valid stored position from an auto-layout, so a
    # graph deployed outside the Studio (no positions) OR one with only a
    # partial positions map doesn't stack un-positioned nodes at the origin.
    need_layout = any(not isinstance(stored_positions.get(n.node_id), dict) for n in graph.nodes)
    layout = _auto_layout(graph) if need_layout else {}
    node_positions = {}
    for node in graph.nodes:
        stored = stored_positions.get(node.node_id)
        node_positions[node.node_id] = (
            stored if isinstance(stored, dict) else layout.get(node.node_id, {"x": 0, "y": 0})
        )

    nodes = []
    for node in graph.nodes:
        pos = node_positions.get(node.node_id, {"x": 0, "y": 0})
        nodes.append(
            StudioNodeResponse(
                id=node.node_id,
                type=_studio_type(node),
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
            kind=edge.kind,
            mapping=edge.mapping,
            condition=edge.condition,
            enabled=edge.enabled,
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
        entry_step=graph.entry_step,
        nodes=nodes,
        edges=edges,
        viewport=viewport,
        execution_settings=StudioExecutionSettings(
            max_total_steps=graph.execution_settings.max_total_steps,
            max_total_runtime_seconds=graph.execution_settings.max_total_runtime_seconds,
            max_visits_per_node=graph.execution_settings.max_visits_per_node,
            max_visits_per_edge=graph.execution_settings.max_visits_per_edge,
            default_timeout_seconds=graph.execution_settings.default_timeout_seconds,
        ),
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
    principal = await require_permission(request, Permission.WORKFLOW_READ)
    repo = _get_graph_repository(request)
    graphs = await repo.list(
        tenant_id=principal.tenant_id,
        workspace_id=principal.workspace_id,
    )
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
    principal = await require_permission(request, Permission.WORKFLOW_ADMIN)
    repo = _get_graph_repository(request)
    graph_id = str(uuid4())
    graph = Graph(
        graph_id=graph_id,
        name=body.name,
        tenant_id=principal.tenant_id,
        workspace_id=principal.workspace_id,
        nodes=[],
        edges=[],
        metadata={
            "studio": {
                "viewport": {"x": 0, "y": 0, "zoom": 1},
                "node_positions": {},
            }
        },
    )
    saved = await repo.save(
        graph,
        tenant_id=principal.tenant_id,
        workspace_id=principal.workspace_id,
    )
    return _graph_to_detail(saved)


@router.get("/workflows/{workflow_id}", response_model=WorkflowDetailResponse)
async def get_workflow(workflow_id: str, request: Request) -> WorkflowDetailResponse:
    """Get a workflow with full detail including nodes, edges, and viewport."""
    principal = await require_permission(request, Permission.WORKFLOW_READ)
    repo = _get_graph_repository(request)
    graph = await repo.get(
        workflow_id,
        tenant_id=principal.tenant_id,
        workspace_id=principal.workspace_id,
    )
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
    principal = await require_permission(request, Permission.WORKFLOW_ADMIN)
    repo = _get_graph_repository(request)
    graph = await repo.get(
        workflow_id,
        tenant_id=principal.tenant_id,
        workspace_id=principal.workspace_id,
    )
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

    # Entrypoint authoring: required by the publish validator, so the canvas
    # must be able to set it. Empty string clears the entrypoint.
    if body.entry_step is not None:
        updates["entry_step"] = body.entry_step or None

    if body.execution_settings is not None:
        updates["execution_settings"] = graph.execution_settings.model_copy(
            update=body.execution_settings.model_dump()
        )

    # Structural authoring: build real nodes/edges. nodes+edges are set together
    # so the Graph validator sees a consistent set (edges must reference nodes).
    if body.nodes is not None:
        graph_version_ref = f"{graph.graph_id}@{graph.version}"
        existing_nodes = {n.node_id: n for n in graph.nodes}
        existing_edges = {edge.edge_id: edge for edge in graph.edges}
        updates["nodes"] = [
            _build_node(n, graph_version_ref, existing_nodes.get(n.id)) for n in body.nodes
        ]
        updates["edges"] = canonicalize_if_route_edges(updates["nodes"], [
            _build_edge(edge, existing_edges.get(edge.id)) for edge in (body.edges or [])
        ])
        # The entrypoint node owns the entry step: when the canvas has one,
        # entry_step is derived, never hand-picked.
        entry_nodes = [n for n in updates["nodes"] if isinstance(n, EntrypointNode)]
        if entry_nodes:
            updates["entry_step"] = entry_nodes[0].node_id
    elif body.edges is not None:
        existing_edges = {edge.edge_id: edge for edge in graph.edges}
        updates["edges"] = canonicalize_if_route_edges(graph.nodes, [
            _build_edge(edge, existing_edges.get(edge.id)) for edge in body.edges
        ])

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
        raise HTTPException(status_code=422, detail=[e["msg"] for e in exc.errors()]) from exc
    saved = await repo.save(
        updated_graph,
        tenant_id=principal.tenant_id,
        workspace_id=principal.workspace_id,
    )
    return _graph_to_detail(saved)


@router.post(
    "/workflows/{workflow_id}/publish",
    response_model=WorkflowDetailResponse,
)
async def publish_workflow(workflow_id: str, request: Request) -> WorkflowDetailResponse:
    """Validate and publish the draft, making it immutable and deployable.

    Validation failures return 422 with the structured issue list so the
    canvas can point at the offending node/edge.
    """
    principal = await require_permission(request, Permission.WORKFLOW_ADMIN)
    repo = _get_graph_repository(request)
    # Studio-authored workflows must start with an Entrypoint node — it is the
    # spatial home of entry_step and the workflow's public input contract.
    # (Code-authored graphs publishing via GraphRepository directly may keep a
    # bare entry_step; this rule is a canvas-authoring contract, not a core one.)
    draft = await repo.get(
        workflow_id,
        tenant_id=principal.tenant_id,
        workspace_id=principal.workspace_id,
    )
    if draft is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if draft.status is GraphStatus.DRAFT and not any(
        isinstance(n, EntrypointNode) for n in draft.nodes
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "message": "workflow failed publish validation",
                "issues": [
                    {
                        "severity": "error",
                        "code": "missing_entrypoint_node",
                        "message": (
                            "Add an Entrypoint node — every workflow starts there, "
                            "and it declares the input contract callers must send."
                        ),
                        "node_id": None,
                        "edge_id": None,
                    }
                ],
            },
        )
    preflight = await preflight_workflow(workflow_id, request)
    if not preflight.ready:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "workflow failed mandatory preflight",
                "issues": [issue.model_dump() for issue in preflight.issues],
            },
        )
    try:
        published = await repo.publish(
            workflow_id,
            tenant_id=principal.tenant_id,
            workspace_id=principal.workspace_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Workflow not found") from exc
    except GraphValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "workflow failed publish validation",
                "issues": [
                    {
                        "severity": issue.severity.value,
                        "code": issue.code.value,
                        "message": issue.message,
                        "node_id": issue.node_id,
                        "edge_id": issue.edge_id,
                    }
                    for issue in exc.report.errors
                ],
            },
        ) from exc
    except GraphLifecycleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _graph_to_detail(published)


@router.post(
    "/workflows/{workflow_id}/preflight",
    response_model=WorkflowPreflightResponse,
)
async def preflight_workflow(
    workflow_id: str,
    request: Request,
) -> WorkflowPreflightResponse:
    """Prove structural and dependency readiness without executing any node.

    This deliberately does not call models, tools, connectors, or child graphs.
    Connectivity and live-provider verification remain separately labelled
    evidence because a preflight must be safe to run against side-effecting
    workflows.
    """
    principal = await require_permission(request, Permission.WORKFLOW_READ)
    repo = _get_graph_repository(request)
    graph = await repo.get(
        workflow_id,
        tenant_id=principal.tenant_id,
        workspace_id=principal.workspace_id,
    )
    if graph is None:
        raise HTTPException(status_code=404, detail="Workflow not found")

    # Ask the validator publish will actually enforce with. A bare GraphValidator()
    # carries no contract registry and no mcp_grants_resolver, and graph_validation
    # returns early when that resolver is None -- above the node loop, so ALL three
    # mcp_tool rules (unknown server, capability floor, capability ceiling) are
    # skipped. Preflight then answers ready=true for a graph publish rejects 422.
    # The fallback is for repositories constructed without a validator (test doubles).
    validator = _get_graph_repository(request).validator or GraphValidator()
    report = await validator.validate(graph)
    issues = [
        WorkflowPreflightIssue(
            severity=issue.severity.value,
            code=issue.code.value,
            message=issue.message,
            node_id=issue.node_id,
            edge_id=issue.edge_id,
        )
        for issue in report.issues
        if issue.severity.value in {"error", "warning"}
    ]
    if not any(isinstance(node, EntrypointNode) for node in graph.nodes):
        issues.append(
            WorkflowPreflightIssue(
                severity="error",
                code="missing_entrypoint_node",
                message="Add an Entrypoint node before publishing from Studio.",
            )
        )

    bootstrap = request.app.state.bootstrap
    contract_registry = getattr(bootstrap, "contract_registry", None)
    if contract_registry is not None:
        scoped_contracts = contract_registry.for_scope(
            contract_scope_context(principal.tenant_id, principal.workspace_id)
        )
        for node in graph.nodes:
            for ref in {node.input_contract_ref, node.output_contract_ref} - {None}:
                try:
                    await scoped_contracts.get(ContractReference.parse(ref))
                except Exception:  # registry implementations use KeyError or lookup-specific errors
                    issues.append(
                        WorkflowPreflightIssue(
                            severity="error",
                            code="unresolved_contract_ref",
                            message=f"Contract {ref!r} is not registered in this workspace.",
                            node_id=node.node_id,
                        )
                    )

    memory_registry = getattr(bootstrap, "memory_registry", None)
    connector_refs = set(memory_registry.list()) if memory_registry is not None else set()
    runner = getattr(getattr(bootstrap, "orchestrator", None), "executable_unit_runner", None)
    manifest_registry = getattr(runner, "registry", None)
    manifest_refs = set(manifest_registry.list()) if manifest_registry is not None else set()
    deployment_service = getattr(bootstrap, "deployment_service", None)
    deployed_graph_refs: set[str] = set()
    if deployment_service is not None:
        deployments = await deployment_service.list(
            tenant_id=principal.tenant_id,
            workspace_id=principal.workspace_id,
        )
        deployed_graph_refs = {deployment.deployment_ref for deployment in deployments}
    for node in graph.nodes:
        if isinstance(node, HttpRequestNode) and not node.http_request.url:
            issues.append(
                WorkflowPreflightIssue(
                    severity="error",
                    code="missing_http_request_url",
                    message="Set a private HTTP URL before publishing this node.",
                    node_id=node.node_id,
                )
            )
        if isinstance(node, RetrievalNode) and node.retrieval.connector_ref not in connector_refs:
            issues.append(
                WorkflowPreflightIssue(
                    severity="error",
                    code="unresolved_connector_ref",
                    message=f"Connector {node.retrieval.connector_ref!r} is not registered.",
                    node_id=node.node_id,
                )
            )
        if (
            isinstance(node, ExecutableUnitNode)
            and node.executable_unit.inline_source is None
            and node.executable_unit.manifest_ref not in manifest_refs
        ):
            issues.append(
                WorkflowPreflightIssue(
                    severity="error",
                    code="unresolved_manifest_ref",
                    message=(
                        f"Executable unit {node.executable_unit.manifest_ref!r} "
                        "is not registered."
                    ),
                    node_id=node.node_id,
                )
            )
        if isinstance(node, SubgraphNode) and node.subgraph.graph_ref not in deployed_graph_refs:
            issues.append(
                WorkflowPreflightIssue(
                    severity="error",
                    code="unresolved_subgraph_ref",
                    message=f"Published subgraph {node.subgraph.graph_ref!r} is not available.",
                    node_id=node.node_id,
                )
            )

    return WorkflowPreflightResponse(
        workflow_id=graph.graph_id,
        version=graph.version,
        ready=not any(issue.severity == "error" for issue in issues),
        checks=["static_validation", "contracts", "connectors", "manifests", "subgraphs"],
        issues=issues,
    )


@router.post(
    "/workflows/{workflow_id}/verify-provider",
    response_model=LiveProviderVerificationResponse,
)
async def verify_workflow_providers(
    workflow_id: str,
    body: LiveProviderVerificationRequest,
    request: Request,
) -> LiveProviderVerificationResponse:
    """Run a bounded, explicitly consented probe against each distinct agent model."""
    principal = await require_permission(request, Permission.WORKFLOW_ADMIN)
    if not body.acknowledge_external_call:
        raise HTTPException(
            status_code=422,
            detail=(
                "Live verification calls external providers and may incur cost; "
                "explicit acknowledgement is required."
            ),
        )
    preflight = await preflight_workflow(workflow_id, request)
    if not preflight.ready:
        raise HTTPException(status_code=409, detail="Workflow must pass preflight first.")
    repo = _get_graph_repository(request)
    graph = await repo.get(
        workflow_id,
        tenant_id=principal.tenant_id,
        workspace_id=principal.workspace_id,
    )
    if graph is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    models = list(
        dict.fromkeys(
            node.agent.model_provider for node in graph.nodes if isinstance(node, AgentNode)
        )
    )
    if not models:
        raise HTTPException(status_code=422, detail="Workflow has no agent providers to verify.")
    if len(models) > body.max_models:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Workflow references {len(models)} models; verification is capped "
                f"at {body.max_models}."
            ),
        )

    bootstrap = request.app.state.bootstrap
    strict_campaign_id = getattr(bootstrap, "evaluation_campaign_id", None)
    if strict_campaign_id is not None and (
        body.campaign_id != strict_campaign_id
        or body.operation_id is None
        or body.max_cost_usd is None
    ):
        raise HTTPException(
            status_code=422,
            detail="strict evaluation requires the configured campaign identity",
        )
    adapter = getattr(bootstrap, "provider_verification_adapter", None)
    if adapter is None:
        adapter = LiteLLMProviderAdapter(
            secret_provider=getattr(bootstrap, "secret_provider", None),
            tenant_id=principal.tenant_id,
            allow_env_fallback=True,
        )
    probes: list[LiveProviderProbe] = []
    for model in models:
        probe_operation_id = (
            body.operation_id
            if body.operation_id is not None and len(models) == 1
            else f"{body.operation_id}:{model}"
            if body.operation_id is not None
            else None
        )
        instrumentation = None
        if any(
            value is not None
            for value in (
                body.campaign_id,
                body.operation_id,
                body.run_id,
                body.max_cost_usd,
                body.run_cap_usd,
            )
        ):
            if body.operation_id is None or body.max_cost_usd is None:
                raise HTTPException(
                    status_code=422,
                    detail="operation_id and max_cost_usd must be supplied together",
                )
            instrumentation = getattr(bootstrap, "probe_instrumentation", None)
            if instrumentation is None:
                raise HTTPException(
                    status_code=503,
                    detail="cost reservation control plane unavailable; failing closed",
                )
            estimator = getattr(bootstrap, "cost_estimator", None)
            if estimator is None:
                raise HTTPException(
                    status_code=503,
                    detail="provider probe maximum cannot be priced; failing closed",
                )
            server_max_cost = estimator.estimate(
                model, input_tokens=256, output_tokens=4
            )
            if server_max_cost is None or server_max_cost <= 0:
                raise HTTPException(
                    status_code=503,
                    detail="provider probe maximum is unknown; failing closed",
                )
            if server_max_cost > body.max_cost_usd:
                raise HTTPException(
                    status_code=422,
                    detail="server-calculated provider maximum exceeds acknowledged ceiling",
                )
            server_run_cap = getattr(
                getattr(bootstrap, "orchestrator", None), "per_run_cap_usd", None
            )
            if server_run_cap is None:
                raise HTTPException(
                    status_code=503,
                    detail="server-owned per-run ceiling is not configured; failing closed",
                )
            if body.run_cap_usd is not None and body.run_cap_usd != server_run_cap:
                raise HTTPException(
                    status_code=422,
                    detail="requested run ceiling does not match the server-owned ceiling",
                )
            try:
                await instrumentation.reserve_probe(
                    tenant_id=principal.tenant_id,
                    campaign_id=body.campaign_id,
                    operation_id=probe_operation_id,
                    run_id=body.run_id,
                    max_cost_usd=str(server_max_cost),
                    run_cap_usd=str(server_run_cap),
                    capability_id="studio.provider_verification",
                    implementation_id=model,
                )
            except Exception as exc:
                raise HTTPException(
                    status_code=503,
                    detail="cost reservation refused or unavailable; failing closed",
                ) from exc
        started = time.monotonic()
        try:
            response = await run_provider_with_timeout(
                adapter,
                ProviderRequest(
                    model_name=model,
                    messages=[{"role": "user", "content": "Reply with OK."}],
                    model_params=ModelParams(temperature=0, max_tokens=4),
                    metadata={"purpose": "zeroth_studio_live_verification"},
                ),
                timeout_seconds=body.timeout_seconds,
            )
            usage = response.token_usage
            evidence = None
            if instrumentation is not None:
                actual_cost = response.cost_usd
                measurement = response.cost_measurement
                if actual_cost is None and usage is not None:
                    estimator = getattr(bootstrap, "cost_estimator", None)
                    if estimator is not None:
                        actual_cost = estimator.estimate(
                            model,
                            input_tokens=usage.input_tokens,
                            output_tokens=usage.output_tokens,
                        )
                        measurement = "estimated"
                provider_request_id = next(
                    (
                        str(response.metadata[key])
                        for key in ("provider_request_id", "request_id", "id")
                        if response.metadata.get(key)
                    ),
                    None,
                )
                if actual_cost is None:
                    evidence = await instrumentation.mark_probe_ambiguous(
                        tenant_id=principal.tenant_id,
                        campaign_id=body.campaign_id,
                        operation_id=probe_operation_id,
                        run_id=body.run_id,
                        capability_id="studio.provider_verification",
                        implementation_id=model,
                        latency_ms=int((time.monotonic() - started) * 1000),
                        cleanup_status="pending_reconciliation",
                        provider_request_id=provider_request_id,
                    )
                    probes.append(
                        LiveProviderProbe(
                            model=model,
                            ok=False,
                            latency_ms=int((time.monotonic() - started) * 1000),
                            input_tokens=usage.input_tokens if usage else None,
                            output_tokens=usage.output_tokens if usage else None,
                            error_code="incomplete_measurement",
                            operation_id=probe_operation_id,
                            cost_event_id=evidence.cost_event_id,
                            audit_event_id=f"audit_{evidence.cost_event_id}",
                            cost_measurement=evidence.cost_measurement,
                            provider_request_id=evidence.provider_request_id,
                            cleanup_status=evidence.cleanup_status,
                        )
                    )
                    continue
                evidence = await instrumentation.commit_probe(
                    tenant_id=principal.tenant_id,
                    campaign_id=body.campaign_id,
                    operation_id=probe_operation_id,
                    run_id=body.run_id,
                    capability_id="studio.provider_verification",
                    implementation_id=model,
                    actual_cost_usd=str(actual_cost),
                    cost_measurement=getattr(measurement, "value", measurement),
                    provider_request_id=provider_request_id,
                    cleanup_status="complete",
                    latency_ms=int((time.monotonic() - started) * 1000),
                    input_tokens=usage.input_tokens if usage else None,
                    output_tokens=usage.output_tokens if usage else None,
                )
            probes.append(
                LiveProviderProbe(
                    model=model,
                    ok=response.content is not None,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    input_tokens=usage.input_tokens if usage else None,
                    output_tokens=usage.output_tokens if usage else None,
                    error_code=None if response.content is not None else "empty_response",
                    operation_id=probe_operation_id,
                    cost_event_id=getattr(evidence, "cost_event_id", None),
                    audit_event_id=(
                        f"audit_{evidence.cost_event_id}" if evidence is not None else None
                    ),
                    cost_measurement=getattr(evidence, "cost_measurement", None),
                    estimated_cost_usd=(
                        actual_cost
                        if getattr(measurement, "value", measurement) == "estimated"
                        else None
                    ),
                    provider_request_id=getattr(evidence, "provider_request_id", None),
                    cleanup_status=getattr(evidence, "cleanup_status", None),
                )
            )
        except TimeoutError:
            evidence = None
            if instrumentation is not None:
                evidence = await instrumentation.mark_probe_ambiguous(
                    tenant_id=principal.tenant_id,
                    campaign_id=body.campaign_id,
                    operation_id=probe_operation_id,
                    run_id=body.run_id,
                    capability_id="studio.provider_verification",
                    implementation_id=model,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    cleanup_status="pending_reconciliation",
                )
            probes.append(
                LiveProviderProbe(
                    model=model,
                    ok=False,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    error_code="timeout",
                    operation_id=probe_operation_id,
                    cost_event_id=getattr(evidence, "cost_event_id", None),
                    audit_event_id=(
                        f"audit_{evidence.cost_event_id}" if evidence is not None else None
                    ),
                    cost_measurement=getattr(evidence, "cost_measurement", None),
                    provider_request_id=getattr(evidence, "provider_request_id", None),
                    cleanup_status=getattr(evidence, "cleanup_status", None),
                )
            )
        except Exception:  # Never return provider or credential exception text to the browser.
            evidence = None
            if instrumentation is not None:
                evidence = await instrumentation.mark_probe_ambiguous(
                    tenant_id=principal.tenant_id,
                    campaign_id=body.campaign_id,
                    operation_id=probe_operation_id,
                    run_id=body.run_id,
                    capability_id="studio.provider_verification",
                    implementation_id=model,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    cleanup_status="pending_reconciliation",
                )
            probes.append(
                LiveProviderProbe(
                    model=model,
                    ok=False,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    error_code="provider_error",
                    operation_id=probe_operation_id,
                    cost_event_id=getattr(evidence, "cost_event_id", None),
                    audit_event_id=(
                        f"audit_{evidence.cost_event_id}" if evidence is not None else None
                    ),
                    cost_measurement=getattr(evidence, "cost_measurement", None),
                    provider_request_id=getattr(evidence, "provider_request_id", None),
                    cleanup_status=getattr(evidence, "cleanup_status", None),
                )
            )
    return LiveProviderVerificationResponse(
        workflow_id=graph.graph_id,
        verified=all(probe.ok for probe in probes),
        probes=probes,
        campaign_id=body.campaign_id,
        operation_id=body.operation_id,
    )


@router.get("/contracts", response_model=list[StudioContractResponse])
async def list_contracts(request: Request) -> list[StudioContractResponse]:
    """List registered contracts (latest version each) for contract-ref pickers."""
    principal = await require_permission(request, Permission.WORKFLOW_READ)
    registry = request.app.state.bootstrap.contract_registry.for_scope(
        contract_scope_context(principal.tenant_id, principal.workspace_id)
    )
    contracts: list[StudioContractResponse] = []
    for name in await registry.list_names():
        record = await registry.get(name)
        contracts.append(
            StudioContractResponse(
                name=record.name,
                version=record.version,
                json_schema=record.json_schema,
            )
        )
    return contracts


@router.post("/contracts", response_model=StudioContractResponse, status_code=201)
async def create_contract(
    body: CreateContractRequest,
    request: Request,
) -> StudioContractResponse:
    """Register a schema-only contract authored in the console.

    The schema is arbitrary JSON Schema — payload validation at run ingress
    honors it exactly. Re-registering an existing name creates the next
    version (the picker lists the latest).
    """
    principal = await require_permission(request, Permission.WORKFLOW_ADMIN)
    from jsonschema.exceptions import SchemaError

    registry = request.app.state.bootstrap.contract_registry.for_scope(
        contract_scope_context(principal.tenant_id, principal.workspace_id)
    )
    try:
        record = await registry.register_schema(
            body.name,
            body.json_schema,
            metadata={**body.metadata, "authored_in": "studio"},
        )
    except SchemaError as exc:
        raise HTTPException(status_code=422, detail=f"invalid JSON Schema: {exc.message}") from exc
    return StudioContractResponse(
        name=record.name,
        version=record.version,
        json_schema=record.json_schema,
    )


@router.get("/workflows/{workflow_id}/diff")
async def diff_workflow(
    workflow_id: str,
    left: int,
    right: int,
    request: Request,
) -> dict:
    """Structured diff between two versions of a workflow."""
    principal = await require_permission(request, Permission.WORKFLOW_READ)
    repo = _get_graph_repository(request)
    try:
        diff = await repo.diff(
            workflow_id,
            left,
            right,
            tenant_id=principal.tenant_id,
            workspace_id=principal.workspace_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Workflow version not found") from exc
    return diff.model_dump(mode="json")


@router.post(
    "/workflows/{workflow_id}/clone",
    response_model=WorkflowDetailResponse,
    status_code=201,
)
async def clone_workflow(workflow_id: str, request: Request) -> WorkflowDetailResponse:
    """Clone a published workflow into a new editable draft version."""
    principal = await require_permission(request, Permission.WORKFLOW_ADMIN)
    repo = _get_graph_repository(request)
    try:
        draft = await repo.clone_published_to_draft(
            workflow_id,
            tenant_id=principal.tenant_id,
            workspace_id=principal.workspace_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Workflow not found") from exc
    except GraphLifecycleError as exc:
        raise HTTPException(
            status_code=409,
            detail="Only published workflows can be cloned to a draft.",
        ) from exc
    return _graph_to_detail(draft)


@router.delete("/workflows/{workflow_id}", status_code=204)
async def delete_workflow(workflow_id: str, request: Request) -> Response:
    """Archive a workflow (soft delete)."""
    principal = await require_permission(request, Permission.WORKFLOW_ADMIN)
    repo = _get_graph_repository(request)
    try:
        await repo.archive(
            workflow_id,
            tenant_id=principal.tenant_id,
            workspace_id=principal.workspace_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Workflow not found") from exc
    return Response(status_code=204)


@router.get("/node-types", response_model=list[NodeTypeResponse])
async def list_node_types(request: Request) -> list[NodeTypeResponse]:
    """Return all available node types with their port definitions."""
    _principal = await require_permission(request, Permission.WORKFLOW_READ)
    return _NODE_TYPES
