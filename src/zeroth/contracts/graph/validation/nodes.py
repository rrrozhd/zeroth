"""Node, node-type, and entrypoint validation."""

from __future__ import annotations

from zeroth.contracts.graph.limits import INLINE_SOURCE_MAX_CHARS
from zeroth.contracts.graph.validation.capabilities import CapabilityChecks
from zeroth.contracts.graph.validation.issues import append_issue
from zeroth.contracts.graph.validation.references import (
    all_unique,
    is_ref_like,
    require_ref,
    validate_ref_list,
)
from zeroth.core.graph.models import (
    AgentNode,
    EntrypointNode,
    ExecutableUnitNode,
    Graph,
    HumanApprovalNode,
    Node,
)
from zeroth.core.graph.validation_errors import (
    ValidationCode,
    ValidationIssue,
    ValidationSeverity,
)


def validate_nodes(
    graph: Graph,
    node_map: dict[str, Node],
    issues: list[ValidationIssue],
    *,
    capability_checks: CapabilityChecks,
) -> None:
    """Check all nodes for duplicate IDs and per-node validation issues."""
    node_ids: list[str] = []
    seen_ids: set[str] = set()
    for node in graph.nodes:
        node_ids.append(node.node_id)
        if node.node_id in seen_ids:
            append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.DUPLICATE_NODE_ID,
                message=f"duplicate node id: {node.node_id}",
                graph_id=graph.graph_id,
                node_id=node.node_id,
            )
            continue
        seen_ids.add(node.node_id)
        # Keep a direct lookup table so later edge checks can validate endpoints cheaply.
        node_map[node.node_id] = node
        validate_node(graph.graph_id, node, issues, capability_checks=capability_checks)

    if node_ids and not all_unique(node_ids):
        # The duplicate issue is already recorded per node; this branch just
        # preserves the "unique node IDs" rule in one place for readability.
        return


def validate_node(
    graph_id: str,
    node: Node,
    issues: list[ValidationIssue],
    *,
    capability_checks: CapabilityChecks,
) -> None:
    """Validate a single node's references, contracts, and type-specific data."""
    if not is_ref_like(node.graph_version_ref):
        append_issue(
            issues,
            severity=ValidationSeverity.ERROR,
            code=ValidationCode.INVALID_GRAPH_VERSION_REF,
            message=f"invalid graph version ref on node {node.node_id!r}",
            graph_id=graph_id,
            node_id=node.node_id,
            path=("nodes", node.node_id, "graph_version_ref"),
            details={"ref": node.graph_version_ref},
        )

    require_ref(
        issues,
        graph_id=graph_id,
        node_id=node.node_id,
        code=ValidationCode.MISSING_CONTRACT_REF,
        message="input contract ref is required",
        value=node.input_contract_ref,
        path=("nodes", node.node_id, "input_contract_ref"),
    )
    require_ref(
        issues,
        graph_id=graph_id,
        node_id=node.node_id,
        code=ValidationCode.INVALID_OUTPUT_CONTRACT,
        message="output contract ref is required",
        value=node.output_contract_ref,
        path=("nodes", node.node_id, "output_contract_ref"),
    )

    validate_ref_list(
        issues,
        graph_id=graph_id,
        node_id=node.node_id,
        refs=node.policy_bindings,
        code=ValidationCode.INVALID_POLICY_REF,
        message="invalid node policy reference",
        path=("nodes", node.node_id, "policy_bindings"),
    )
    validate_ref_list(
        issues,
        graph_id=graph_id,
        node_id=node.node_id,
        refs=node.capability_bindings,
        code=ValidationCode.INVALID_CAPABILITY_REF,
        message="invalid capability reference",
        path=("nodes", node.node_id, "capability_bindings"),
    )

    match node:
        case AgentNode():
            validate_agent_node(graph_id, node, issues, capability_checks=capability_checks)
        case ExecutableUnitNode():
            validate_executable_unit_node(graph_id, node, issues)
        case HumanApprovalNode():
            # Approval nodes have their own checks because they pause and resume execution.
            validate_human_approval_node(graph_id, node, issues)


def validate_agent_node(
    graph_id: str,
    node: AgentNode,
    issues: list[ValidationIssue],
    *,
    capability_checks: CapabilityChecks,
) -> None:
    """Check agent-specific fields like instruction, model provider, and tool refs."""
    if not node.agent.instruction.strip():
        append_issue(
            issues,
            severity=ValidationSeverity.ERROR,
            code=ValidationCode.INVALID_NODE_ATTACHMENT,
            message="agent instruction is required",
            graph_id=graph_id,
            node_id=node.node_id,
            path=("nodes", node.node_id, "agent", "instruction"),
        )
    if not node.agent.model_provider.strip():
        append_issue(
            issues,
            severity=ValidationSeverity.ERROR,
            code=ValidationCode.INVALID_NODE_ATTACHMENT,
            message="agent model provider is required",
            graph_id=graph_id,
            node_id=node.node_id,
            path=("nodes", node.node_id, "agent", "model_provider"),
        )
    if node.agent.persist_conversation and not node.agent.input_messages_key:
        # Without a messages field there are no turns to persist — a
        # silent no-op the author almost certainly did not intend.
        append_issue(
            issues,
            severity=ValidationSeverity.ERROR,
            code=ValidationCode.INVALID_NODE_ATTACHMENT,
            message="persist_conversation requires input_messages_key to be set",
            graph_id=graph_id,
            node_id=node.node_id,
            path=("nodes", node.node_id, "agent", "persist_conversation"),
        )
    validate_ref_list(
        issues,
        graph_id=graph_id,
        node_id=node.node_id,
        refs=node.agent.tool_refs,
        code=ValidationCode.INVALID_NODE_ATTACHMENT,
        message="invalid tool reference",
        path=("nodes", node.node_id, "agent", "tool_refs"),
    )
    validate_ref_list(
        issues,
        graph_id=graph_id,
        node_id=node.node_id,
        refs=node.agent.memory_refs,
        code=ValidationCode.INVALID_NODE_ATTACHMENT,
        message="invalid memory reference",
        path=("nodes", node.node_id, "agent", "memory_refs"),
    )
    capability_checks.validate_agent_capabilities(graph_id, node, issues)


def validate_executable_unit_node(
    graph_id: str,
    node: ExecutableUnitNode,
    issues: list[ValidationIssue],
) -> None:
    """Check executable-unit-specific fields like the manifest reference.

    Inline units (the Studio code node) carry source instead of a manifest
    ref — for those, gate publish on the source itself: present, within
    the size cap, and syntactically valid Python.
    """
    if node.executable_unit.inline_source is not None:
        validate_inline_source(graph_id, node, issues)
        return
    if not node.executable_unit.manifest_ref.strip():
        append_issue(
            issues,
            severity=ValidationSeverity.ERROR,
            code=ValidationCode.INVALID_NODE_ATTACHMENT,
            message="executable unit manifest ref is required",
            graph_id=graph_id,
            node_id=node.node_id,
            path=("nodes", node.node_id, "executable_unit", "manifest_ref"),
        )


def validate_inline_source(
    graph_id: str,
    node: ExecutableUnitNode,
    issues: list[ValidationIssue],
) -> None:
    """Publish gate for authored code: non-empty, capped, compilable."""
    source = node.executable_unit.inline_source or ""
    path = ("nodes", node.node_id, "executable_unit", "inline_source")
    if not source.strip():
        append_issue(
            issues,
            severity=ValidationSeverity.ERROR,
            code=ValidationCode.INVALID_INLINE_SOURCE,
            message="code is required",
            graph_id=graph_id,
            node_id=node.node_id,
            path=path,
        )
        return
    if len(source) > INLINE_SOURCE_MAX_CHARS:
        append_issue(
            issues,
            severity=ValidationSeverity.ERROR,
            code=ValidationCode.INVALID_INLINE_SOURCE,
            message=f"code exceeds the {INLINE_SOURCE_MAX_CHARS} character limit",
            graph_id=graph_id,
            node_id=node.node_id,
            path=path,
        )
        return
    try:
        compile(source, f"<code node {node.node_id}>", "exec")
    except SyntaxError as exc:
        append_issue(
            issues,
            severity=ValidationSeverity.ERROR,
            code=ValidationCode.INVALID_INLINE_SOURCE,
            message=f"syntax error on line {exc.lineno}: {exc.msg}",
            graph_id=graph_id,
            node_id=node.node_id,
            path=path,
        )


def validate_human_approval_node(
    graph_id: str,
    node: HumanApprovalNode,
    issues: list[ValidationIssue],
) -> None:
    """Check approval-node-specific fields like schema references."""
    require_ref(
        issues,
        graph_id=graph_id,
        node_id=node.node_id,
        code=ValidationCode.INVALID_NODE_ATTACHMENT,
        message="approval payload schema ref is required",
        value=node.human_approval.approval_payload_schema_ref,
        path=("nodes", node.node_id, "human_approval", "approval_payload_schema_ref"),
    )
    require_ref(
        issues,
        graph_id=graph_id,
        node_id=node.node_id,
        code=ValidationCode.INVALID_NODE_ATTACHMENT,
        message="resolution schema ref is required",
        value=node.human_approval.resolution_schema_ref,
        path=("nodes", node.node_id, "human_approval", "resolution_schema_ref"),
    )


def validate_entrypoint(
    graph: Graph,
    node_map: dict[str, Node],
    issues: list[ValidationIssue],
) -> None:
    """Check that the graph has an entry step and that it points to a real node."""
    if graph.entry_step is None:
        append_issue(
            issues,
            severity=ValidationSeverity.ERROR,
            code=ValidationCode.MISSING_ENTRYPOINT,
            message="graph entrypoint is required",
            graph_id=graph.graph_id,
            path=("entry_step",),
        )
        return
    if graph.entry_step not in node_map:
        append_issue(
            issues,
            severity=ValidationSeverity.ERROR,
            code=ValidationCode.UNKNOWN_ENTRYPOINT,
            message=f"entrypoint node does not exist: {graph.entry_step}",
            graph_id=graph.graph_id,
            path=("entry_step",),
            details={"entry_step": graph.entry_step},
        )
        return
    validate_entrypoint_nodes(graph, issues)


def validate_entrypoint_nodes(graph: Graph, issues: list[ValidationIssue]) -> None:
    """Structural rules for dedicated entrypoint nodes.

    A graph needs at most one; when present it must BE the entry step and
    nothing may flow into it. (Presence itself is a Studio-authoring rule,
    enforced at the studio publish route — code-authored graphs may keep a
    bare entry_step.)
    """
    entry_nodes = [node for node in graph.nodes if isinstance(node, EntrypointNode)]
    if not entry_nodes:
        return
    for extra in entry_nodes[1:]:
        append_issue(
            issues,
            severity=ValidationSeverity.ERROR,
            code=ValidationCode.INVALID_NODE_ATTACHMENT,
            message="a workflow can only have one entrypoint node",
            graph_id=graph.graph_id,
            node_id=extra.node_id,
            path=("nodes", extra.node_id),
        )
    primary = entry_nodes[0]
    if graph.entry_step != primary.node_id:
        append_issue(
            issues,
            severity=ValidationSeverity.ERROR,
            code=ValidationCode.UNKNOWN_ENTRYPOINT,
            message="entry_step must point at the entrypoint node",
            graph_id=graph.graph_id,
            node_id=primary.node_id,
            path=("entry_step",),
            details={"entry_step": graph.entry_step, "entrypoint": primary.node_id},
        )
    entry_ids = {node.node_id for node in entry_nodes}
    for edge in graph.edges:
        if edge.target_node_id in entry_ids:
            append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.INVALID_NODE_ATTACHMENT,
                message="the entrypoint node cannot have incoming edges",
                graph_id=graph.graph_id,
                node_id=edge.target_node_id,
                edge_id=edge.edge_id,
                path=("edges", edge.edge_id),
            )
