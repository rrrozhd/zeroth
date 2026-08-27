"""The public graph validator: contract checks plus execution checks.

``GraphValidator`` is the entry point for publish-time validation. Most of what
it runs is contract-owned and lives in
:mod:`zeroth.contracts.graph.validation`; this module adds the two concerns the
contracts layer may not import.

* **Parallel config** resolves ``reducer_ref`` through the runtime reducer
  registry, so it is runtime work by definition.
* **Capability grants** resolve refs against the governance ``Capability``
  enum. That enum cannot move -- its module path is embedded in signature
  strings pinned by the immutable legacy library surface -- so the rules are
  supplied to the contract validators through the ``CapabilityChecks`` seam
  and fire at the positions they have always occupied.

Composing here rather than in ``contracts`` is what lets the contracts package
stay free of runtime and governance imports. ``zeroth.contracts.graph.validation``
re-exports this class, resolved lazily.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from zeroth.contracts.graph.models import (
    AgentNode,
    ExecutableUnitNode,
    Graph,
    MCPToolNode,
    Node,
)
from zeroth.contracts.graph.validation import ContractValidator
from zeroth.contracts.graph.validation.issues import append_issue
from zeroth.contracts.graph.validation_errors import (
    GraphValidationReport,
    ValidationCode,
    ValidationIssue,
    ValidationSeverity,
)
from zeroth.contracts.mappings import MappingValidator
from zeroth.contracts.registry import ContractRegistry
from zeroth.governance.policy.models import Capability

# The floor is imported from the module that enforces it at dispatch rather
# than respelled here. It used to be a literal in two places in this file, a
# third in ``service/mcp_import.py`` and a fourth in the tests, so "publish
# demands exactly what the pool demands" was four copies agreeing by habit.
#
# ``zeroth.runtime.agents.__init__`` eagerly imports the provider stack, so
# naming a submodule under it pulls litellm in. Importing *this module alone*
# therefore went from 472 to 2775 modules. That number is not the cost, though,
# and it is worth not being misread as one: the only two runtime importers of
# this validator are ``service/bootstrap/factory.py`` and ``service/demo.py``,
# both of which build agent runners and so already loaded the provider stack --
# measured, ``import zeroth.service.bootstrap.factory`` is 3802 modules with
# litellm resident both before and after this import edge existed, and
# ``zeroth.service.cli`` resolves the validator lazily and is unchanged at 166.
# What actually regressed is importing the validator in isolation, which only
# test collection and contract-only consumers do. If that ever has to go, the
# fix is making the agents package init lazy -- not respelling the pair here.
from zeroth.runtime.agents.mcp import MCP_REQUIRED_CAPABILITIES
from zeroth.runtime.parallel.errors import ReducerRefValidationError
from zeroth.runtime.parallel.reducers import resolve_reducer_ref


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


#: Resolves an MCP ``server_ref`` to the capabilities its operator declared,
#: or ``None`` when this deployment has no such server registered. Async because
#: the registry is a database table, not an in-process dict.
MCPGrantsResolver = Callable[[str], Awaitable[set[Capability] | None]]


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
        mcp_grants_resolver: MCPGrantsResolver | None = None,
    ):
        self._contract_registry = contract_registry
        # Resolves an ``mcp_tool`` node's server_ref to the operator-declared
        # ceiling. Optional for the same reason contract_registry is: callers
        # doing contract-only validation have no deployment to resolve against.
        self._mcp_grants_resolver = mcp_grants_resolver
        # ``self`` is the CapabilityChecks implementation, so the governance
        # rules below run at their original positions inside the contract
        # validators rather than as a separate pass afterwards.
        self._contract_validator = ContractValidator(
            mapping_validator=mapping_validator,
            capability_checks=self,
        )

    async def validate(self, graph: Graph) -> GraphValidationReport:
        """Run all validation checks and return a report of any issues found.

        Async because parallel-config validation (Phase 43) may call
        ``ContractRegistry.get`` which is async.
        """
        issues: list[ValidationIssue] = []
        self._contract_validator.validate(graph, issues)
        await self._validate_parallel_configs(graph, issues)
        await self._validate_mcp_tool_grants(graph, issues)

        return GraphValidationReport(graph_id=graph.graph_id, issues=issues)

    async def validate_or_raise(self, graph: Graph) -> GraphValidationReport:
        """Validate the graph and raise GraphValidationError if there are errors."""
        report = await self.validate(graph)
        report.raise_for_errors()
        return report

    async def _validate_mcp_tool_grants(
        self,
        graph: Graph,
        issues: list[ValidationIssue],
    ) -> None:
        """Hold every ``mcp_tool`` node inside its server's operator-declared ceiling.

        This is the check the registry exists for. ``capability_bindings`` are
        author-declared -- ``PolicyGuard`` resolves required capabilities from
        the node and lets policies (bound in the same graph) decide only whether
        they are permitted -- so without an operator-owned side, an author who
        wants a capability simply writes it. The server's ``grants`` are the one
        side of this comparison the author cannot edit.

        With no resolver wired this pass is skipped, matching the posture the
        class already takes toward ``contract_registry``: contract-only callers
        have no deployment to resolve a ref against.
        """
        if self._mcp_grants_resolver is None:
            return
        for node in graph.nodes:
            if not isinstance(node, MCPToolNode):
                continue
            granted = await self._mcp_grants_resolver(node.mcp_tool.server_ref)
            if granted is None:
                append_issue(
                    issues,
                    severity=ValidationSeverity.ERROR,
                    code=ValidationCode.INVALID_NODE_ATTACHMENT,
                    message=(
                        f"mcp_tool node {node.node_id!r} references unknown MCP server "
                        f"{node.mcp_tool.server_ref!r}; register it before publishing"
                    ),
                    graph_id=graph.graph_id,
                    node_id=node.node_id,
                    path=("nodes", node.node_id, "mcp_tool", "server_ref"),
                    details={"server_ref": node.mcp_tool.server_ref},
                )
                continue
            declared = _capabilities_from_refs(node.capability_bindings)
            # The floor. Starting an MCP server spawns a subprocess that talks
            # to an external service, and MCPSessionPool demands both
            # unconditionally before it will hand out a session. Checking only
            # a ceiling here made declaring *nothing* the cheapest way past
            # publish -- it left `excess` empty -- and then the run failed at
            # dispatch instead, so publish and runtime disagreed about the same
            # node. It also made "grants=[] denies every referencing node" false
            # at publish, since a node declaring nothing exceeded nothing.
            missing = sorted(
                capability.value for capability in (MCP_REQUIRED_CAPABILITIES - declared)
            )
            if missing:
                append_issue(
                    issues,
                    severity=ValidationSeverity.ERROR,
                    code=ValidationCode.MISSING_MCP_CAPABILITY,
                    message=(
                        f"mcp_tool node {node.node_id!r} is missing {', '.join(missing)}; "
                        "reaching an MCP server spawns a subprocess that calls out to an "
                        "external service, so both are required"
                    ),
                    graph_id=graph.graph_id,
                    node_id=node.node_id,
                    path=("nodes", node.node_id, "capability_bindings"),
                    details={"missing_capabilities": missing},
                )
            excess = sorted(capability.value for capability in (declared - granted))
            if excess:
                append_issue(
                    issues,
                    severity=ValidationSeverity.ERROR,
                    code=ValidationCode.INVALID_CAPABILITY_REF,
                    message=(
                        f"mcp_tool node {node.node_id!r} declares {', '.join(excess)}, which "
                        f"MCP server {node.mcp_tool.server_ref!r} does not grant; an operator "
                        "must widen the server's grants or the node must ask for less"
                    ),
                    graph_id=graph.graph_id,
                    node_id=node.node_id,
                    path=("nodes", node.node_id, "capability_bindings"),
                    details={
                        "excess_capabilities": excess,
                        "server_ref": node.mcp_tool.server_ref,
                    },
                )

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
            # Superseded by mcp_tool nodes. This path lets the graph author pick
            # the binary, argv and env themselves, with no operator-owned row to
            # bound it -- the exact gap the registry exists to close -- and the
            # tools it reaches are discovered at run time, so nothing about them
            # is knowable here. Warned rather than rejected: existing graphs
            # keep working until the removal in a later high-tier bump.
            append_issue(
                issues,
                severity=ValidationSeverity.WARNING,
                code=ValidationCode.DEPRECATED_MCP_SERVERS,
                message=(
                    f"agent {node.node_id!r} declares inline mcp_servers, which is "
                    "deprecated: the server's command is author-controlled and its tools "
                    "are unpinned. Register the server with an operator (POST "
                    "/v1/mcp/servers) and import its tools with `zeroth-core mcp-import`"
                ),
                graph_id=graph_id,
                node_id=node.node_id,
                path=("nodes", node.node_id, "agent", "mcp_servers"),
                details={"replacement": "mcp_tool"},
            )
            # MCP servers are spawned subprocesses that call out to external
            # services; publishing a graph whose agent lacks either capability
            # would only fail later, at dispatch on an enforced deployment.
            granted = _capabilities_from_refs(node.capability_bindings)
            missing = sorted(cap.value for cap in (MCP_REQUIRED_CAPABILITIES - granted))
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

        The required set for a tool is its target node's declared
        ``capability_bindings`` unioned with the binding's own
        ``required_capabilities``. The target arm must stay identical to
        ``factory.tool_required_capabilities`` -- ``ExecutableUnitNode |
        MCPToolNode``, not just the former. While this checked only executable
        units, an ``mcp_tool`` target contributed nothing here, so an agent was
        never required at publish to hold what the runner gate demands: exactly
        what ``zeroth-core mcp-import`` produces, which writes the spawn pair
        onto the ``mcp_tool`` node and nothing onto the agent, published with
        zero errors and was then denied at its first tool call.

        What that identity buys is that no tool's requirement is *invisible*
        here: both sides read the same source. It is not a promise that a graph
        passing this check cannot be denied at dispatch. The subjects differ --
        this compares the agent's author-declared ``capability_bindings``, while
        ``ToolBridge.check_capabilities`` compares the effective set
        ``PolicyGuard`` yields for the node, which a policy bound in the same
        graph can make smaller. Surfaced at author time so the gap that *is*
        knowable here is fixed on the canvas rather than as a run-time denial.
        """
        granted = _capabilities_from_refs(node.capability_bindings)
        for binding in node.agent.tool_bindings:
            required = set(binding.required_capabilities)
            target = node_map.get(binding.target_node_id)
            if isinstance(target, ExecutableUnitNode | MCPToolNode):
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
