"""Check a graph for problems before it can be published or run.

The GraphValidator inspects nodes, edges, conditions, and structure to find
issues like missing references, duplicate IDs, invalid conditions, and
unsafe cycles.  It produces a report listing all problems found.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from zeroth.core.contracts.registry import ContractRegistry
from zeroth.core.graph.models import (
    AgentNode,
    Edge,
    ExecutableUnitNode,
    Graph,
    HumanApprovalNode,
    Node,
)
from zeroth.core.graph.validation_errors import (
    GraphValidationReport,
    ValidationCode,
    ValidationIssue,
    ValidationSeverity,
)
from zeroth.core.mappings import MappingValidationError, MappingValidator
from zeroth.core.parallel.errors import ReducerRefValidationError
from zeroth.core.parallel.reducers import resolve_reducer_ref


def _is_ref_like(value: str) -> bool:
    """Return True if the string looks like a valid reference (non-empty, no spaces)."""
    text = value.strip()
    return bool(text) and not any(part.isspace() for part in text)


def _append_issue(
    issues: list[ValidationIssue],
    *,
    severity: ValidationSeverity,
    code: ValidationCode,
    message: str,
    graph_id: str,
    node_id: str | None = None,
    edge_id: str | None = None,
    path: tuple[str, ...] = (),
    details: dict[str, Any] | None = None,
) -> None:
    """Helper to create a ValidationIssue and add it to the issues list."""
    issues.append(
        ValidationIssue(
            severity=severity,
            code=code,
            message=message,
            graph_id=graph_id,
            node_id=node_id,
            edge_id=edge_id,
            path=path,
            details=dict(details or {}),
        )
    )


def _all_unique(values: Iterable[str]) -> bool:
    """Return True if every string in the iterable is unique (no duplicates)."""
    seen: set[str] = set()
    for value in values:
        if value in seen:
            return False
        seen.add(value)
    return True


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
            _append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.EMPTY_GRAPH,
                message="graph must contain at least one node",
                graph_id=graph.graph_id,
            )

        self._validate_graph_refs(graph, issues)
        self._validate_nodes(graph, node_map, issues)
        self._validate_entrypoint(graph, node_map, issues)
        self._validate_edges(graph, node_map, edge_ids, adjacency, issues)
        self._validate_cycles(graph, node_map, adjacency, issues)
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
                    _append_issue(
                        issues,
                        severity=ValidationSeverity.ERROR,
                        code=ValidationCode.INVALID_REDUCER_REF,
                        message=(
                            f"invalid reducer_ref on node {node.node_id!r}: {exc}"
                        ),
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
            _append_issue(
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
            _append_issue(
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
            contract_version = await self._contract_registry.get(
                node.output_contract_ref
            )
        except Exception as exc:  # noqa: BLE001 - any registry failure is validation-fatal
            _append_issue(
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
            _append_issue(
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

    def _validate_graph_refs(self, graph: Graph, issues: list[ValidationIssue]) -> None:
        """Check that graph-level policy references look valid."""
        for ref in graph.policy_bindings:
            if not _is_ref_like(ref):
                _append_issue(
                    issues,
                    severity=ValidationSeverity.ERROR,
                    code=ValidationCode.INVALID_POLICY_REF,
                    message=f"invalid policy reference: {ref!r}",
                    graph_id=graph.graph_id,
                    path=("policy_bindings",),
                    details={"ref": ref},
                )

    def _validate_nodes(
        self,
        graph: Graph,
        node_map: dict[str, Node],
        issues: list[ValidationIssue],
    ) -> None:
        """Check all nodes for duplicate IDs and per-node validation issues."""
        node_ids: list[str] = []
        seen_ids: set[str] = set()
        for node in graph.nodes:
            node_ids.append(node.node_id)
            if node.node_id in seen_ids:
                _append_issue(
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
            self._validate_node(graph.graph_id, node, issues)

        if node_ids and not _all_unique(node_ids):
            # The duplicate issue is already recorded per node; this branch just
            # preserves the "unique node IDs" rule in one place for readability.
            return

    def _validate_node(self, graph_id: str, node: Node, issues: list[ValidationIssue]) -> None:
        """Validate a single node's references, contracts, and type-specific data."""
        if not _is_ref_like(node.graph_version_ref):
            _append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.INVALID_GRAPH_VERSION_REF,
                message=f"invalid graph version ref on node {node.node_id!r}",
                graph_id=graph_id,
                node_id=node.node_id,
                path=("nodes", node.node_id, "graph_version_ref"),
                details={"ref": node.graph_version_ref},
            )

        self._require_ref(
            issues,
            graph_id=graph_id,
            node_id=node.node_id,
            code=ValidationCode.MISSING_CONTRACT_REF,
            message="input contract ref is required",
            value=node.input_contract_ref,
            path=("nodes", node.node_id, "input_contract_ref"),
        )
        self._require_ref(
            issues,
            graph_id=graph_id,
            node_id=node.node_id,
            code=ValidationCode.INVALID_OUTPUT_CONTRACT,
            message="output contract ref is required",
            value=node.output_contract_ref,
            path=("nodes", node.node_id, "output_contract_ref"),
        )

        self._validate_ref_list(
            issues,
            graph_id=graph_id,
            node_id=node.node_id,
            refs=node.policy_bindings,
            code=ValidationCode.INVALID_POLICY_REF,
            message="invalid node policy reference",
            path=("nodes", node.node_id, "policy_bindings"),
        )
        self._validate_ref_list(
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
                self._validate_agent_node(graph_id, node, issues)
            case ExecutableUnitNode():
                self._validate_executable_unit_node(graph_id, node, issues)
            case HumanApprovalNode():
                # Approval nodes have their own checks because they pause and resume execution.
                self._validate_human_approval_node(graph_id, node, issues)

    def _validate_agent_node(
        self,
        graph_id: str,
        node: AgentNode,
        issues: list[ValidationIssue],
    ) -> None:
        """Check agent-specific fields like instruction, model provider, and tool refs."""
        if not node.agent.instruction.strip():
            _append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.INVALID_NODE_ATTACHMENT,
                message="agent instruction is required",
                graph_id=graph_id,
                node_id=node.node_id,
                path=("nodes", node.node_id, "agent", "instruction"),
            )
        if not node.agent.model_provider.strip():
            _append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.INVALID_NODE_ATTACHMENT,
                message="agent model provider is required",
                graph_id=graph_id,
                node_id=node.node_id,
                path=("nodes", node.node_id, "agent", "model_provider"),
            )
        self._validate_ref_list(
            issues,
            graph_id=graph_id,
            node_id=node.node_id,
            refs=node.agent.tool_refs,
            code=ValidationCode.INVALID_NODE_ATTACHMENT,
            message="invalid tool reference",
            path=("nodes", node.node_id, "agent", "tool_refs"),
        )
        self._validate_ref_list(
            issues,
            graph_id=graph_id,
            node_id=node.node_id,
            refs=node.agent.memory_refs,
            code=ValidationCode.INVALID_NODE_ATTACHMENT,
            message="invalid memory reference",
            path=("nodes", node.node_id, "agent", "memory_refs"),
        )

    def _validate_executable_unit_node(
        self,
        graph_id: str,
        node: ExecutableUnitNode,
        issues: list[ValidationIssue],
    ) -> None:
        """Check executable-unit-specific fields like the manifest reference."""
        if not node.executable_unit.manifest_ref.strip():
            _append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.INVALID_NODE_ATTACHMENT,
                message="executable unit manifest ref is required",
                graph_id=graph_id,
                node_id=node.node_id,
                path=("nodes", node.node_id, "executable_unit", "manifest_ref"),
            )

    def _validate_human_approval_node(
        self,
        graph_id: str,
        node: HumanApprovalNode,
        issues: list[ValidationIssue],
    ) -> None:
        """Check approval-node-specific fields like schema references."""
        self._require_ref(
            issues,
            graph_id=graph_id,
            node_id=node.node_id,
            code=ValidationCode.INVALID_NODE_ATTACHMENT,
            message="approval payload schema ref is required",
            value=node.human_approval.approval_payload_schema_ref,
            path=("nodes", node.node_id, "human_approval", "approval_payload_schema_ref"),
        )
        self._require_ref(
            issues,
            graph_id=graph_id,
            node_id=node.node_id,
            code=ValidationCode.INVALID_NODE_ATTACHMENT,
            message="resolution schema ref is required",
            value=node.human_approval.resolution_schema_ref,
            path=("nodes", node.node_id, "human_approval", "resolution_schema_ref"),
        )

    def _validate_entrypoint(
        self,
        graph: Graph,
        node_map: dict[str, Node],
        issues: list[ValidationIssue],
    ) -> None:
        """Check that the graph has an entry step and that it points to a real node."""
        if graph.entry_step is None:
            _append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.MISSING_ENTRYPOINT,
                message="graph entrypoint is required",
                graph_id=graph.graph_id,
                path=("entry_step",),
            )
            return
        if graph.entry_step not in node_map:
            _append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.UNKNOWN_ENTRYPOINT,
                message=f"entrypoint node does not exist: {graph.entry_step}",
                graph_id=graph.graph_id,
                path=("entry_step",),
                details={"entry_step": graph.entry_step},
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
                _append_issue(
                    issues,
                    severity=ValidationSeverity.ERROR,
                    code=ValidationCode.DUPLICATE_EDGE_ID,
                    message=f"duplicate edge id: {edge.edge_id}",
                    graph_id=graph.graph_id,
                    edge_id=edge.edge_id,
                )
            edge_ids.add(edge.edge_id)

            if edge.source_node_id not in node_map:
                _append_issue(
                    issues,
                    severity=ValidationSeverity.ERROR,
                    code=ValidationCode.UNKNOWN_EDGE_SOURCE,
                    message=f"edge source does not exist: {edge.source_node_id}",
                    graph_id=graph.graph_id,
                    edge_id=edge.edge_id,
                    path=("edges", edge.edge_id, "source_node_id"),
                    details={"source_node_id": edge.source_node_id},
                )
            else:
                adjacency[edge.source_node_id].append(edge.target_node_id)

            if edge.target_node_id not in node_map:
                _append_issue(
                    issues,
                    severity=ValidationSeverity.ERROR,
                    code=ValidationCode.UNKNOWN_EDGE_TARGET,
                    message=f"edge target does not exist: {edge.target_node_id}",
                    graph_id=graph.graph_id,
                    edge_id=edge.edge_id,
                    path=("edges", edge.edge_id, "target_node_id"),
                    details={"target_node_id": edge.target_node_id},
                )

            if edge.condition is not None:
                self._validate_condition(graph.graph_id, edge, issues)

            if edge.mapping is not None:
                self._validate_mapping(graph.graph_id, edge, issues)

    def _validate_condition(
        self,
        graph_id: str,
        edge: Edge,
        issues: list[ValidationIssue],
    ) -> None:
        """Check that an edge's condition has a non-empty expression and valid operand refs."""
        condition = edge.condition
        assert condition is not None
        if not condition.expression.strip():
            _append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.INVALID_CONDITION,
                message="condition expression is required",
                graph_id=graph_id,
                edge_id=edge.edge_id,
                path=("edges", edge.edge_id, "condition", "expression"),
            )
        self._validate_ref_list(
            issues,
            graph_id=graph_id,
            edge_id=edge.edge_id,
            refs=condition.operand_refs,
            code=ValidationCode.INVALID_CONDITION,
            message="invalid condition operand reference",
            path=("edges", edge.edge_id, "condition", "operand_refs"),
        )

    def _validate_mapping(
        self,
        graph_id: str,
        edge: Edge,
        issues: list[ValidationIssue],
    ) -> None:
        """Validate an edge's data mapping using the mapping validator."""
        try:
            self._mapping_validator.validate(edge.mapping)  # type: ignore[arg-type]
        except MappingValidationError as exc:
            _append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.INVALID_MAPPING,
                message=str(exc),
                graph_id=graph_id,
                edge_id=edge.edge_id,
                path=("edges", edge.edge_id, "mapping"),
                details={"error": str(exc)},
            )

    def _validate_cycles(
        self,
        graph: Graph,
        node_map: dict[str, Node],
        adjacency: dict[str, list[str]],
        issues: list[ValidationIssue],
    ) -> None:
        """Detect unsafe cycles in the graph.

        Cycles are allowed only when the graph has a configured safeguard that
        prevents infinite execution.
        """
        components = self._strongly_connected_components(node_map.keys(), adjacency)
        for component in components:
            if len(component) == 1:
                node_id = next(iter(component))
                if node_id not in adjacency.get(node_id, []):
                    continue

            component_edges = [
                edge
                for edge in graph.edges
                if edge.enabled
                and edge.source_node_id in component
                and edge.target_node_id in component
            ]
            if self._component_has_safeguard(graph, component_edges):
                continue

            _append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.UNSAFE_CYCLE,
                message="cyclic graph path must declare a safeguard",
                graph_id=graph.graph_id,
                details={
                    "nodes": sorted(component),
                    "edges": [edge.edge_id for edge in component_edges],
                },
            )

    def _component_has_safeguard(self, graph: Graph, edges: list[Edge]) -> bool:
        """Return True if a cycle has something preventing infinite loops."""
        if graph.execution_settings.max_visits_per_edge is not None:
            return True
        return any(edge.condition and edge.condition.allow_cycle_traversal for edge in edges)

    def _strongly_connected_components(
        self,
        node_ids: Iterable[str],
        adjacency: dict[str, list[str]],
    ) -> list[set[str]]:
        """Find all groups of nodes that can reach each other (Tarjan's algorithm).

        Each group returned is a set of node IDs that form a cycle.
        Single nodes without self-loops are also returned but filtered later.
        """
        index = 0
        stack: list[str] = []
        on_stack: set[str] = set()
        indices: dict[str, int] = {}
        lowlinks: dict[str, int] = {}
        components: list[set[str]] = []

        def strongconnect(node_id: str) -> None:
            nonlocal index
            indices[node_id] = index
            lowlinks[node_id] = index
            index += 1
            stack.append(node_id)
            on_stack.add(node_id)

            for neighbour in adjacency.get(node_id, []):
                if neighbour not in indices:
                    strongconnect(neighbour)
                    lowlinks[node_id] = min(lowlinks[node_id], lowlinks[neighbour])
                elif neighbour in on_stack:
                    lowlinks[node_id] = min(lowlinks[node_id], indices[neighbour])

            if lowlinks[node_id] == indices[node_id]:
                component: set[str] = set()
                while stack:
                    current = stack.pop()
                    on_stack.remove(current)
                    component.add(current)
                    if current == node_id:
                        break
                components.append(component)

        for node_id in node_ids:
            if node_id not in indices:
                strongconnect(node_id)

        return components

    def _require_ref(
        self,
        issues: list[ValidationIssue],
        *,
        graph_id: str,
        node_id: str,
        code: ValidationCode,
        message: str,
        value: str | None,
        path: tuple[str, ...],
    ) -> None:
        """Record an error if a required reference is missing or invalid."""
        if value is None or not _is_ref_like(value):
            _append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=code,
                message=message,
                graph_id=graph_id,
                node_id=node_id,
                path=path,
                details={"ref": value},
            )

    def _validate_ref_list(
        self,
        issues: list[ValidationIssue],
        *,
        graph_id: str,
        refs: list[str],
        code: ValidationCode,
        message: str,
        path: tuple[str, ...],
        node_id: str | None = None,
        edge_id: str | None = None,
    ) -> None:
        """Check each reference in a list and record an error for any invalid ones."""
        for index, ref in enumerate(refs):
            if not _is_ref_like(ref):
                _append_issue(
                    issues,
                    severity=ValidationSeverity.ERROR,
                    code=code,
                    message=message,
                    graph_id=graph_id,
                    node_id=node_id,
                    edge_id=edge_id,
                    path=path + (str(index),),
                    details={"ref": ref},
                )
