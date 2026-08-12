"""Node, node-type, and entrypoint validation."""

from __future__ import annotations

from zeroth.contracts.graph.limits import (
    AGENT_INSTRUCTION_MAX_CHARS,
    DESCRIPTION_MAX_CHARS,
    DISPLAY_LABEL_MAX_CHARS,
    DISPLAY_TAG_MAX_CHARS,
    DISPLAY_TITLE_MAX_CHARS,
    INLINE_SOURCE_MAX_CHARS,
)
from zeroth.contracts.graph.models import (
    AgentNode,
    DisplayMetadata,
    EntrypointNode,
    ExecutableUnitNode,
    Graph,
    HumanApprovalNode,
    Node,
)
from zeroth.contracts.graph.validation.capabilities import CapabilityChecks
from zeroth.contracts.graph.validation.issues import append_issue
from zeroth.contracts.graph.validation.references import (
    all_unique,
    is_ref_like,
    require_ref,
    validate_ref_list,
)
from zeroth.contracts.graph.validation_errors import (
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
    _validate_display_metadata(graph_id, node.node_id, node.display, issues)
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


def _validate_display_metadata(
    graph_id: str,
    node_id: str,
    display: DisplayMetadata,
    issues: list[ValidationIssue],
) -> None:
    """Bound UI metadata at publish time while keeping historical rows loadable."""

    def check(value: str | None, limit: int, label: str, path: tuple[str, ...]) -> None:
        if value is not None and len(value) > limit:
            append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.INVALID_NODE_ATTACHMENT,
                message=f"display {label} exceeds the {limit} character limit",
                graph_id=graph_id,
                node_id=node_id,
                path=("nodes", node_id, "display", *path),
            )

    check(display.title, DISPLAY_TITLE_MAX_CHARS, "title", ("title",))
    check(display.description, DESCRIPTION_MAX_CHARS, "description", ("description",))
    for index, tag in enumerate(display.tags):
        check(tag, DISPLAY_TAG_MAX_CHARS, "tag", ("tags", str(index)))
    for key, value in display.labels.items():
        check(key, DISPLAY_LABEL_MAX_CHARS, "label key", ("labels", key))
        check(value, DISPLAY_LABEL_MAX_CHARS, "label value", ("labels", key))


def _validate_description_length(
    description: str,
    issues: list[ValidationIssue],
    *,
    graph_id: str,
    node_id: str,
    label: str,
    path: tuple[str, ...],
) -> None:
    """Bound one author-written description (A05-5).

    Descriptions are already required to be non-empty by the model; what they
    lacked was a ceiling. Every one of them is copied verbatim into the tool
    schema sent to the provider on each step.
    """
    if len(description) > DESCRIPTION_MAX_CHARS:
        append_issue(
            issues,
            severity=ValidationSeverity.ERROR,
            code=ValidationCode.INVALID_TOOL_BINDING,
            message=f"{label} exceeds the {DESCRIPTION_MAX_CHARS} character limit",
            graph_id=graph_id,
            node_id=node_id,
            path=path,
        )


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
    elif len(node.agent.instruction) > AGENT_INSTRUCTION_MAX_CHARS:
        # A05-5: the instruction is author-written text that reaches the model as
        # prompt content on every step of every run, and travels in the graph
        # payload row. Bounded here, at publish, for the same reason
        # inline_source is.
        append_issue(
            issues,
            severity=ValidationSeverity.ERROR,
            code=ValidationCode.INVALID_NODE_ATTACHMENT,
            message=(
                f"agent instruction exceeds the {AGENT_INSTRUCTION_MAX_CHARS} character limit"
            ),
            graph_id=graph_id,
            node_id=node.node_id,
            path=("nodes", node.node_id, "agent", "instruction"),
        )
    for index, binding in enumerate(node.agent.tool_bindings):
        _validate_description_length(
            binding.description,
            issues,
            graph_id=graph_id,
            node_id=node.node_id,
            label=f"tool binding {binding.name!r} description",
            path=("nodes", node.node_id, "agent", "tool_bindings", str(index), "description"),
        )
        for arg_index, argument in enumerate(binding.arguments):
            _validate_description_length(
                argument.description,
                issues,
                graph_id=graph_id,
                node_id=node.node_id,
                label=f"tool argument {argument.name!r} description",
                path=(
                    "nodes",
                    node.node_id,
                    "agent",
                    "tool_bindings",
                    str(index),
                    "arguments",
                    str(arg_index),
                    "description",
                ),
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
