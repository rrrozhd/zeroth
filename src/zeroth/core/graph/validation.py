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
from zeroth.contracts.graph.validation.edges import validate_edges
from zeroth.contracts.graph.validation.issues import append_issue
from zeroth.contracts.graph.validation.nodes import (
    validate_entrypoint,
    validate_nodes,
)
from zeroth.contracts.graph.validation.references import (
    validate_graph_refs,
)
from zeroth.contracts.graph.validation.tools import validate_tool_attachments
from zeroth.core.contracts.registry import ContractRegistry
from zeroth.core.graph.models import (
    AgentNode,
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
        validate_edges(
            graph,
            node_map,
            edge_ids,
            adjacency,
            issues,
            mapping_validator=self._mapping_validator,
        )
        validate_tool_attachments(graph, node_map, issues, capability_checks=self)
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

    def validate_tool_grants(
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
