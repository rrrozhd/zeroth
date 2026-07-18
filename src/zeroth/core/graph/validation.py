"""Check a graph for problems before it can be published or run.

The GraphValidator inspects nodes, edges, conditions, and structure to find
issues like missing references, duplicate IDs, invalid conditions, and
unsafe cycles.  It produces a report listing all problems found.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from zeroth.contracts.graph.validation.cycles import validate_cycles
from zeroth.contracts.graph.validation.issues import append_issue
from zeroth.contracts.graph.validation.mappings import (
    validate_condition,
    validate_mapping,
)
from zeroth.contracts.graph.validation.nodes import (
    validate_entrypoint,
    validate_nodes,
)
from zeroth.contracts.graph.validation.references import (
    validate_graph_refs,
)
from zeroth.core.contracts.registry import ContractRegistry
from zeroth.core.graph.models import (
    AgentNode,
    Edge,
    ExecutableUnitNode,
    Graph,
    Node,
)
from zeroth.core.graph.validation_errors import (
    GraphValidationReport,
    ValidationCode,
    ValidationIssue,
    ValidationSeverity,
)
from zeroth.core.mappings import MappingValidator
from zeroth.core.parallel.errors import ReducerRefValidationError
from zeroth.core.parallel.reducers import resolve_reducer_ref
from zeroth.core.policy.models import Capability


def _capabilities_from_refs(refs: Iterable[str]) -> set[Capability]:
    """Map capability_binding refs to Capabilities, dropping non-value refs.

    Matches the runner factory's ``_capability_from_ref`` so author-time
    validation and runtime enforcement resolve the identical set.
    """
    caps: set[Capability] = set()
    for ref in refs:
        try:
            caps.add(Capability(ref))
        except ValueError:
            continue
    return caps


class GraphValidator:
    """Check a graph for structural and reference errors.

    This validator only looks at the graph itself -- it does not check
    whether referenced contracts, policies, or tools actually exist in
    external registries.
    """

    def __init__(
        self,
        mapping_validator: MappingValidator | None = None,
        contract_registry: ContractRegistry | None = None,
    ):
        self._mapping_validator = mapping_validator or MappingValidator()
        self._contract_registry = contract_registry

    async def validate(self, graph: Graph) -> GraphValidationReport:
        """Run all validation checks and return a report of any issues found.

        Async because parallel-config validation (Phase 43) may call
        ``ContractRegistry.get`` which is async.
        """
        issues: list[ValidationIssue] = []
        node_map: dict[str, Node] = {}
        edge_ids: set[str] = set()
        # Adjacency is built once and then reused by the cycle checks later in validation.
        adjacency: dict[str, list[str]] = defaultdict(list)

        if not graph.nodes:
            append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.EMPTY_GRAPH,
                message="graph must contain at least one node",
                graph_id=graph.graph_id,
            )

        validate_graph_refs(graph, issues)
        validate_nodes(graph, node_map, issues, capability_checks=self)
        validate_entrypoint(graph, node_map, issues)
        self._validate_edges(graph, node_map, edge_ids, adjacency, issues)
        self._validate_tool_attachments(graph, node_map, issues)
        validate_cycles(graph, node_map, adjacency, issues)
        await self._validate_parallel_configs(graph, issues)

        return GraphValidationReport(graph_id=graph.graph_id, issues=issues)

    async def validate_or_raise(self, graph: Graph) -> GraphValidationReport:
        """Validate the graph and raise GraphValidationError if there are errors."""
        report = await self.validate(graph)
        report.raise_for_errors()
        return report

    async def _validate_parallel_configs(
        self,
        graph: Graph,
        issues: list[ValidationIssue],
    ) -> None:
        """Publish-time checks for ParallelConfig on each node (Phase 43-02).

        * ``custom`` strategies: resolve ``reducer_ref`` via
          ``resolve_reducer_ref`` (D-16 full import + callable check).
        * ``merge`` strategies: verify node's output contract has top-level
          ``type == "object"`` via injected ``ContractRegistry`` (D-17). If
          no registry is wired, degrade with a WARNING rather than ERROR so
          tests and bootstraps without a registry are not broken.

        The Pydantic model validator on ``ParallelConfig`` already enforces
        strategy/reducer_ref consistency (D-04); this method does only the
        deeper semantic checks that require external context.
        """
        for node in graph.nodes:
            cfg = getattr(node, "parallel_config", None)
            if cfg is None:
                continue
            if cfg.merge_strategy == "custom":
                try:
                    resolve_reducer_ref(cfg.reducer_ref)  # type: ignore[arg-type]
                except ReducerRefValidationError as exc:
                    append_issue(
                        issues,
                        severity=ValidationSeverity.ERROR,
                        code=ValidationCode.INVALID_REDUCER_REF,
                        message=(f"invalid reducer_ref on node {node.node_id!r}: {exc}"),
                        graph_id=graph.graph_id,
                        node_id=node.node_id,
                        path=("nodes", node.node_id, "parallel_config", "reducer_ref"),
                        details={"reducer_ref": cfg.reducer_ref},
                    )
            if cfg.merge_strategy == "merge":
                await self._check_merge_dict_contract(graph, node, issues)

    async def _check_merge_dict_contract(
        self,
        graph: Graph,
        node: Any,
        issues: list[ValidationIssue],
    ) -> None:
        """Verify a node's output contract is dict-shaped for merge strategy (D-17)."""
        if self._contract_registry is None:
            append_issue(
                issues,
                severity=ValidationSeverity.WARNING,
                code=ValidationCode.INVALID_MERGE_STRATEGY,
                message=(
                    f"merge_strategy='merge' on node {node.node_id!r} cannot be "
                    "contract-checked because no ContractRegistry is wired; "
                    "dict-shape will be enforced at runtime instead"
                ),
                graph_id=graph.graph_id,
                node_id=node.node_id,
                path=("nodes", node.node_id, "parallel_config", "merge_strategy"),
            )
            return
        if not node.output_contract_ref:
            append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.INVALID_MERGE_STRATEGY,
                message=(
                    f"merge_strategy='merge' on node {node.node_id!r} requires "
                    "output_contract_ref to be set so the output shape can be "
                    "verified as dict-like"
                ),
                graph_id=graph.graph_id,
                node_id=node.node_id,
                path=("nodes", node.node_id, "output_contract_ref"),
            )
            return
        try:
            contract_version = await self._contract_registry.get(node.output_contract_ref)
        except Exception as exc:  # noqa: BLE001 - any registry failure is validation-fatal
            append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.INVALID_MERGE_STRATEGY,
                message=(
                    f"merge_strategy='merge' on node {node.node_id!r}: could not "
                    f"resolve output_contract_ref "
                    f"{node.output_contract_ref!r}: {exc}"
                ),
                graph_id=graph.graph_id,
                node_id=node.node_id,
                path=("nodes", node.node_id, "output_contract_ref"),
                details={"error": str(exc)},
            )
            return
        schema_type = contract_version.json_schema.get("type")
        if schema_type != "object":
            append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.INVALID_MERGE_STRATEGY,
                message=(
                    f"merge_strategy='merge' on node {node.node_id!r} requires "
                    f"an output contract with top-level type='object', got "
                    f"type={schema_type!r} from contract "
                    f"{node.output_contract_ref!r}"
                ),
                graph_id=graph.graph_id,
                node_id=node.node_id,
                path=("nodes", node.node_id, "parallel_config", "merge_strategy"),
                details={"schema_type": schema_type},
            )

    def validate_agent_capabilities(
        self,
        graph_id: str,
        node: AgentNode,
        issues: list[ValidationIssue],
    ) -> None:
        """Governance-owned capability rules for an agent node."""
        if node.agent.mcp_servers:
            # MCP servers are spawned subprocesses that call out to external
            # services; publishing a graph whose agent lacks either capability
            # would only fail later, at dispatch on an enforced deployment.
            granted = _capabilities_from_refs(node.capability_bindings)
            required = {Capability.PROCESS_SPAWN, Capability.EXTERNAL_API_CALL}
            missing = sorted(cap.value for cap in (required - granted))
            if missing:
                append_issue(
                    issues,
                    severity=ValidationSeverity.ERROR,
                    code=ValidationCode.MISSING_MCP_CAPABILITY,
                    message=(
                        f"agent {node.node_id!r} declares mcp_servers but is missing "
                        f"{', '.join(missing)}; add the missing capabilities to the "
                        "agent's capability_bindings"
                    ),
                    graph_id=graph_id,
                    node_id=node.node_id,
                    path=("nodes", node.node_id, "agent", "mcp_servers"),
                    details={"missing_capabilities": missing},
                )

    def _validate_edges(
        self,
        graph: Graph,
        node_map: dict[str, Node],
        edge_ids: set[str],
        adjacency: dict[str, list[str]],
        issues: list[ValidationIssue],
    ) -> None:
        """Validate edge wiring and edge-level payloads.

        This checks for duplicate IDs, unknown source or target nodes, and
        invalid condition or mapping payloads.
        """
        for edge in graph.edges:
            if edge.edge_id in edge_ids:
                append_issue(
                    issues,
                    severity=ValidationSeverity.ERROR,
                    code=ValidationCode.DUPLICATE_EDGE_ID,
                    message=f"duplicate edge id: {edge.edge_id}",
                    graph_id=graph.graph_id,
                    edge_id=edge.edge_id,
                )
            edge_ids.add(edge.edge_id)

            if edge.source_node_id not in node_map:
                append_issue(
                    issues,
                    severity=ValidationSeverity.ERROR,
                    code=ValidationCode.UNKNOWN_EDGE_SOURCE,
                    message=f"edge source does not exist: {edge.source_node_id}",
                    graph_id=graph.graph_id,
                    edge_id=edge.edge_id,
                    path=("edges", edge.edge_id, "source_node_id"),
                    details={"source_node_id": edge.source_node_id},
                )
            elif edge.kind != "tool":
                # Tool edges attach tools rather than route execution, so they
                # stay out of the control-flow adjacency (and cycle checks).
                adjacency[edge.source_node_id].append(edge.target_node_id)

            if edge.target_node_id not in node_map:
                append_issue(
                    issues,
                    severity=ValidationSeverity.ERROR,
                    code=ValidationCode.UNKNOWN_EDGE_TARGET,
                    message=f"edge target does not exist: {edge.target_node_id}",
                    graph_id=graph.graph_id,
                    edge_id=edge.edge_id,
                    path=("edges", edge.edge_id, "target_node_id"),
                    details={"target_node_id": edge.target_node_id},
                )

            if edge.kind == "tool":
                self._validate_tool_edge(graph.graph_id, edge, node_map, issues)

            if edge.condition is not None:
                validate_condition(graph.graph_id, edge, issues)

            if edge.mapping is not None:
                validate_mapping(
                    graph.graph_id,
                    edge,
                    issues,
                    mapping_validator=self._mapping_validator,
                )

    def _validate_tool_edge(
        self,
        graph_id: str,
        edge: Edge,
        node_map: dict[str, Node],
        issues: list[ValidationIssue],
    ) -> None:
        """Check a tool edge's endpoints: agent source, executable-unit target.

        Conditions and mappings belong to control flow; a tool edge carrying
        either is a sign the author meant a data edge.
        """
        source = node_map.get(edge.source_node_id)
        if source is not None and not isinstance(source, AgentNode):
            append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.INVALID_TOOL_EDGE,
                message="tool edge source must be an agent node",
                graph_id=graph_id,
                edge_id=edge.edge_id,
                path=("edges", edge.edge_id, "source_node_id"),
                details={"source_node_id": edge.source_node_id},
            )
        target = node_map.get(edge.target_node_id)
        if target is not None and not isinstance(target, ExecutableUnitNode):
            append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.INVALID_TOOL_EDGE,
                message="tool edge target must be an executable unit or code node",
                graph_id=graph_id,
                edge_id=edge.edge_id,
                path=("edges", edge.edge_id, "target_node_id"),
                details={"target_node_id": edge.target_node_id},
            )
        if edge.condition is not None or edge.mapping is not None:
            append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.INVALID_TOOL_EDGE,
                message="tool edges cannot carry conditions or mappings",
                graph_id=graph_id,
                edge_id=edge.edge_id,
                path=("edges", edge.edge_id),
            )

    def _validate_tool_attachments(
        self,
        graph: Graph,
        node_map: dict[str, Node],
        issues: list[ValidationIssue],
    ) -> None:
        """Cross-check tool edges against each agent's tool bindings.

        Every attached unit needs exactly one author-provided binding (name,
        description, argument descriptions — enforced by the binding model),
        names must be unique per agent, and bindings must not point at units
        that are no longer attached.
        """
        tool_targets: dict[str, list[str]] = defaultdict(list)
        for edge in graph.edges:
            if edge.kind == "tool" and edge.enabled:
                tool_targets[edge.source_node_id].append(edge.target_node_id)

        for node in graph.nodes:
            if not isinstance(node, AgentNode):
                continue
            attached = tool_targets.get(node.node_id, [])
            bound_targets = [binding.target_node_id for binding in node.agent.tool_bindings]

            for target_id in attached:
                if bound_targets.count(target_id) == 0:
                    append_issue(
                        issues,
                        severity=ValidationSeverity.ERROR,
                        code=ValidationCode.INVALID_TOOL_BINDING,
                        message=(
                            f"attached tool {target_id!r} needs a binding with a "
                            "name, description, and argument descriptions"
                        ),
                        graph_id=graph.graph_id,
                        node_id=node.node_id,
                        path=("nodes", node.node_id, "agent", "tool_bindings"),
                        details={"target_node_id": target_id},
                    )
                elif bound_targets.count(target_id) > 1:
                    append_issue(
                        issues,
                        severity=ValidationSeverity.ERROR,
                        code=ValidationCode.INVALID_TOOL_BINDING,
                        message=f"attached tool {target_id!r} has multiple bindings",
                        graph_id=graph.graph_id,
                        node_id=node.node_id,
                        path=("nodes", node.node_id, "agent", "tool_bindings"),
                        details={"target_node_id": target_id},
                    )

            for binding in node.agent.tool_bindings:
                if binding.target_node_id not in attached:
                    append_issue(
                        issues,
                        severity=ValidationSeverity.ERROR,
                        code=ValidationCode.INVALID_TOOL_BINDING,
                        message=(
                            f"tool binding {binding.name!r} points at "
                            f"{binding.target_node_id!r}, which is not attached by a tool edge"
                        ),
                        graph_id=graph.graph_id,
                        node_id=node.node_id,
                        path=("nodes", node.node_id, "agent", "tool_bindings"),
                        details={"target_node_id": binding.target_node_id},
                    )

            names = [binding.name for binding in node.agent.tool_bindings]
            duplicates = sorted({name for name in names if names.count(name) > 1})
            if duplicates:
                append_issue(
                    issues,
                    severity=ValidationSeverity.ERROR,
                    code=ValidationCode.INVALID_TOOL_BINDING,
                    message=f"tool names must be unique per agent: {', '.join(duplicates)}",
                    graph_id=graph.graph_id,
                    node_id=node.node_id,
                    path=("nodes", node.node_id, "agent", "tool_bindings"),
                    details={"duplicate_names": duplicates},
                )

            self._validate_tool_capability_grants(graph, node, node_map, issues)

    def _validate_tool_capability_grants(
        self,
        graph: Graph,
        node: AgentNode,
        node_map: dict[str, Node],
        issues: list[ValidationIssue],
    ) -> None:
        """WS-C: an agent's capability grant must cover every attached tool's needs.

        The required set for a tool is its target unit node's declared
        ``capability_bindings`` unioned with the binding's own
        ``required_capabilities`` — the SAME source the runner factory uses at
        runtime, so a graph that passes here cannot be denied at dispatch for an
        under-granted capability (and vice versa). Surfaced at author time so the
        gap is fixed on the canvas rather than as a run-time denial.
        """
        granted = _capabilities_from_refs(node.capability_bindings)
        for binding in node.agent.tool_bindings:
            required = set(binding.required_capabilities)
            target = node_map.get(binding.target_node_id)
            if isinstance(target, ExecutableUnitNode):
                required |= _capabilities_from_refs(target.capability_bindings)
            missing = sorted(cap.value for cap in (required - granted))
            if missing:
                append_issue(
                    issues,
                    severity=ValidationSeverity.ERROR,
                    code=ValidationCode.CAPABILITY_GRANT_INSUFFICIENT,
                    message=(
                        f"agent {node.node_id!r} grants "
                        f"{sorted(cap.value for cap in granted)} but tool "
                        f"{binding.name!r} requires {', '.join(missing)}; add the "
                        "missing capabilities to the agent's capability_bindings"
                    ),
                    graph_id=graph.graph_id,
                    node_id=node.node_id,
                    path=("nodes", node.node_id, "agent", "tool_bindings"),
                    details={"tool": binding.name, "missing_capabilities": missing},
                )
