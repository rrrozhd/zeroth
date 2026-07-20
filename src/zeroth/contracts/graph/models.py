"""Core data models that define what a graph looks like.

A graph is made up of nodes (the steps) and edges (the connections between
steps).  This module contains all the Pydantic models that represent those
pieces, plus helper enums and settings objects.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from zeroth.contracts.governed.app.spec import (
    GovernedFlowSpec,
    GovernedStepSpec,
    TransitionSpec,
    branch,
    end,
    route_to,
    then,
)
from zeroth.contracts.mappings.models import EdgeMapping
from zeroth.contracts.templates.models import TemplateReference
from zeroth.platform.primitives import utc_now


class Capability(StrEnum):
    """A specific permission that a node might need to do its job.

    Each value represents one kind of action (like reading from the network
    or writing to the filesystem). Authored tool bindings declare the
    capabilities they require, and policies use these values to control
    what nodes are allowed to do; the policy engine republishes the enum
    from :mod:`zeroth.governance.policy.models`.
    """

    NETWORK_READ = "network_read"
    NETWORK_WRITE = "network_write"
    FILESYSTEM_READ = "filesystem_read"
    FILESYSTEM_WRITE = "filesystem_write"
    SECRET_ACCESS = "secret_access"
    EXTERNAL_API_CALL = "external_api_call"
    PROCESS_SPAWN = "process_spawn"
    MEMORY_READ = "memory_read"
    MEMORY_WRITE = "memory_write"


class GraphStatus(StrEnum):
    """The lifecycle stage of a graph version.

    Graphs start as DRAFT, get PUBLISHED when ready, and can be ARCHIVED
    when no longer needed.
    """

    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class DisplayMetadata(BaseModel):
    """Human-readable labels and tags shown in the UI for a node or graph."""

    title: str | None = None
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)


class ExecutionSettings(BaseModel):
    """Safety limits and behavior settings for running a graph.

    These settings prevent runaway execution by capping the number of steps,
    total runtime, and visits per node or edge.
    """

    max_total_steps: int = Field(default=1000, ge=1)
    max_total_runtime_seconds: int | None = Field(default=None, ge=1)
    max_visits_per_node: int = Field(default=10, ge=1)
    max_visits_per_edge: int | None = Field(default=None, ge=1)
    default_timeout_seconds: int | None = Field(default=None, ge=1)
    failure_policy: str = "fail_fast"
    audit_enabled: bool = True
    sequential_join_enabled: bool = False
    """B9 feature flag. When False (default) the runtime dispatches downstream
    nodes exactly as it always has — the join-barrier dispatch/merge path is
    dormant and execution is byte-identical to pre-B9. When True, a convergent
    node (>1 non-tool inbound control-flow edge that is not a ``parallel_config``
    fan-in) is dispatched once per iteration after all its inbound edges resolve
    (delivered or suppressed), with delivered payloads merged via its
    ``JoinConfig``. Opt-in until the parallel/loop/interrupt suites soak."""


class ContextWindowSettings(BaseModel):
    """Per-node context window configuration.

    Controls when and how the tracker compacts messages to stay within
    the model's context window limit. Authored on agent node data; the
    runtime context-window tracker consumes it.

    Fields:
        max_context_tokens: Maximum context window size in tokens.
            Set to 0 to disable compaction entirely.
        summary_trigger_ratio: Ratio of accumulated tokens to max that
            triggers compaction. Must be > 0 and <= 1.
        compaction_strategy: Name of the strategy to use for compaction.
        preserve_recent_messages_count: Number of recent messages to keep
            untouched during compaction.
        archive_originals: When True, compaction strategies store the
            original (dropped/modified) messages in CompactionResult.
    """

    model_config = ConfigDict(extra="forbid")

    max_context_tokens: int = Field(default=128_000, ge=0)
    summary_trigger_ratio: float = Field(default=0.8, gt=0.0, le=1.0)
    compaction_strategy: str = "observation_masking"
    preserve_recent_messages_count: int = Field(default=4, ge=0)
    archive_originals: bool = False


class ParallelConfig(BaseModel):
    """Configuration for parallel fan-out on a node.

    Specifies how to split output into branches, how to merge results,
    what to do when a branch fails, and an optional cap on branch count.
    Authored on node data; the runtime parallel executor consumes it.
    """

    model_config = ConfigDict(extra="forbid")

    split_path: str
    """Dot-path to the list in the node's output that should be split."""

    merge_strategy: Literal["collect", "reduce", "merge", "custom"] = "collect"
    """How branch outputs are combined (D-04 literal):
    'collect' gathers into a list, 'reduce' applies the built-in
    last-wins fold, 'merge' shallow-merges dicts in branch order, and
    'custom' applies a user-supplied dotted-path reducer."""

    reducer_ref: str | None = None
    """Dotted import path to a user-supplied reducer callable. Only valid
    with ``merge_strategy='custom'`` (D-04). The callable must accept two
    positional arguments ``(accumulator, next_value)`` and return the new
    accumulator. Rejected at publish time if the path cannot be resolved
    to a callable."""

    fail_mode: Literal["fail_fast", "best_effort"] = "fail_fast"
    """Behavior on branch failure: 'fail_fast' cancels remaining branches
    on the first error, 'best_effort' runs all branches and collects errors."""

    max_branches: int | None = Field(default=None, ge=1)
    """Optional cap on the number of parallel branches. None means unlimited."""

    @model_validator(mode="after")
    def _validate_reducer_ref_consistency(self) -> ParallelConfig:
        """Enforce D-04 literal: only ``custom`` requires ``reducer_ref``.

        ``reduce`` uses a built-in default fold and MUST NOT carry a
        ``reducer_ref``. All other strategies (``collect``, ``merge``) also
        reject ``reducer_ref``. This keeps ``reduce`` and ``custom``
        semantically distinct.
        """
        if self.merge_strategy == "custom" and not self.reducer_ref:
            raise ValueError("merge_strategy='custom' requires reducer_ref to be set")
        if self.merge_strategy != "custom" and self.reducer_ref is not None:
            raise ValueError(
                "reducer_ref is only valid with merge_strategy='custom', "
                f"got merge_strategy={self.merge_strategy!r}"
            )
        return self


class JoinConfig(BaseModel):
    """Merge policy for a sequential *join* (convergent) node (B9).

    A convergent node reached by more than one non-tool control-flow edge that
    is *not* a ``parallel_config`` fan-in needs to declare how the payloads of
    its delivered inbound edges combine when >1 deliver in the same iteration.
    This reuses the exact ``merge_strategy``/``reducer_ref`` vocabulary as
    :class:`ParallelConfig` and dispatches through the same
    ``parallel.reducers.dispatch_strategy`` registry — the join subsystem does
    NOT reinvent merge semantics.

    Unlike ``ParallelConfig`` there is no ``split_path``/``fail_mode``/
    ``max_branches``: a join has no fan-out list to split and no per-branch
    failure handling; it simply reduces the ordered list of delivered inbound
    payloads.

    Default ``merge_strategy='merge'`` (shallow dict merge, last delivered edge
    wins on key conflict) — the least-surprise behaviour for a plain diamond.
    """

    model_config = ConfigDict(extra="forbid")

    merge_strategy: Literal["collect", "reduce", "merge", "custom"] = "merge"
    """How delivered inbound payloads combine (same vocabulary as
    ``ParallelConfig``): 'merge' shallow-merges dicts in inbound-edge order,
    'reduce' applies the built-in last-wins fold, 'collect' gathers into a list,
    and 'custom' applies a user-supplied dotted-path reducer."""

    reducer_ref: str | None = None
    """Dotted import path to a user-supplied reducer callable. Only valid with
    ``merge_strategy='custom'`` (mirrors ``ParallelConfig``)."""

    @model_validator(mode="after")
    def _validate_reducer_ref_consistency(self) -> JoinConfig:
        """Only ``custom`` may (and must) carry a ``reducer_ref`` (mirrors ParallelConfig)."""
        if self.merge_strategy == "custom" and not self.reducer_ref:
            raise ValueError("merge_strategy='custom' requires reducer_ref to be set")
        if self.merge_strategy != "custom" and self.reducer_ref is not None:
            raise ValueError(
                "reducer_ref is only valid with merge_strategy='custom', "
                f"got merge_strategy={self.merge_strategy!r}"
            )
        return self


class SubgraphNodeData(BaseModel):
    """Configuration for a subgraph invocation step.

    Specifies which published graph to invoke as a child workflow,
    how threads are shared, and the maximum nesting depth allowed.
    Embedded inside ``SubgraphNode``; the runtime subgraph executor
    consumes it.
    """

    model_config = ConfigDict(extra="forbid")

    graph_ref: str
    """Name of the published graph to invoke."""

    version: int | None = None
    """Specific deployment version; None means latest active."""

    thread_participation: Literal["inherit", "isolated"] = "inherit"
    """Whether the child run shares the parent's thread or gets its own."""

    max_depth: int = Field(default=3, ge=1, le=10)
    """Maximum recursion depth for nested subgraph invocations."""


class Condition(BaseModel):
    """A rule that decides whether an edge should be followed.

    Conditions are attached to edges and evaluated at runtime to determine
    which path the execution should take next.
    """

    expression: str
    operand_refs: list[str] = Field(default_factory=list)
    branch_rule: Literal["all", "any", "expression"] = "expression"
    allow_cycle_traversal: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class NodeBase(BaseModel):
    """Shared fields that every type of node has.

    You won't create this directly -- use AgentNode, ExecutableUnitNode,
    or HumanApprovalNode instead.
    """

    model_config = ConfigDict(extra="forbid")

    node_id: str
    graph_version_ref: str
    node_version: int = Field(default=1, ge=1)
    display: DisplayMetadata = Field(default_factory=DisplayMetadata)
    input_contract_ref: str | None = None
    output_contract_ref: str | None = None
    execution_config: dict[str, Any] = Field(default_factory=dict)
    policy_bindings: list[str] = Field(default_factory=list)
    capability_bindings: list[str] = Field(default_factory=list)
    audit_config: dict[str, Any] = Field(default_factory=dict)
    parallel_config: ParallelConfig | None = None
    join_config: JoinConfig | None = None
    """B9 merge policy for a convergent (join) node. Required at publish time
    (only when ``execution_settings.sequential_join_enabled`` is set) on a node
    with >=2 unconditional non-tool inbound edges — genuine concurrent delivery.
    Conditional reconvergence (mutually-exclusive inbound) needs no JoinConfig."""

    def to_governed_step_spec(self) -> GovernedStepSpec:
        """Convert this node into a GovernedStepSpec for the execution engine."""
        raise NotImplementedError


class TemplateMemoryBinding(BaseModel):
    """Declares which memory connector values to inject into the template memory namespace.

    Each binding pulls one value (get mode) or a set of values by prefix (scan mode)
    from a named connector and exposes them as ``memory.<as_name>`` in prompt templates.
    ``connector_instance_id`` must match one of the values in the parent
    ``AgentNodeData.memory_refs`` list.
    """

    as_name: str
    connector_instance_id: str
    access_mode: Literal["get", "scan"] = "get"
    key: str | None = None
    key_prefix: str | None = None
    default: Any = None
    max_items: int | None = Field(default=None, ge=1)
    scope: Literal["run", "thread", "shared"] = "run"

    @model_validator(mode="after")
    def _validate_mode_fields(self) -> TemplateMemoryBinding:
        if self.access_mode == "get" and self.key is None:
            msg = "key is required when access_mode is 'get'"
            raise ValueError(msg)
        return self


_TOOL_NAME_PATTERN = r"^[a-zA-Z0-9_-]{1,64}$"


class ToolArgument(BaseModel):
    """One argument of a tool exposed to an agent.

    The description is mandatory: the model only sees the JSON schema built
    from these entries, so an undescribed argument is unusable in practice.
    """

    name: str = Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$")
    type: Literal["string", "number", "integer", "boolean", "object", "array"] = "string"
    description: str = Field(min_length=1)
    required: bool = True

    def to_schema_property(self) -> dict[str, Any]:
        """Render this argument as a JSON Schema property."""
        return {"type": self.type, "description": self.description}


class AgentToolBinding(BaseModel):
    """Exposes an attached executable-unit node as a callable tool.

    The attachment itself is a tool-kind edge from the agent to the unit;
    this binding carries what the model needs to call it — an alias distinct
    from the node id, a description, and described arguments. All three are
    author-provided, never derived, so the model never sees internal ids.
    """

    target_node_id: str
    name: str = Field(pattern=_TOOL_NAME_PATTERN)
    description: str = Field(min_length=1)
    arguments: list[ToolArgument] = Field(default_factory=list)
    # WS-C: extra capabilities this tool requires, UNIONED with (never replacing)
    # the authoritative set derived from the target unit's own declared
    # capabilities. Author-facing escape hatch for targets that carry none of
    # their own — chiefly ``mcp://`` tools, whose target is an external server
    # rather than a graph node.
    required_capabilities: list[Capability] = Field(default_factory=list)

    def parameters_schema(self) -> dict[str, Any]:
        """Compile the argument list into a JSON Schema object for tool calling."""
        return {
            "type": "object",
            "properties": {arg.name: arg.to_schema_property() for arg in self.arguments},
            "required": [arg.name for arg in self.arguments if arg.required],
            "additionalProperties": False,
        }

    @model_validator(mode="after")
    def _validate_arguments(self) -> AgentToolBinding:
        names = [arg.name for arg in self.arguments]
        if len(names) != len(set(names)):
            raise ValueError("tool argument names must be unique")
        return self


class AgentNodeData(BaseModel):
    """Configuration for an AI agent step.

    Holds the instruction prompt, which model to use, what tools and memory
    the agent can access, and other agent-specific settings.
    """

    instruction: str
    model_provider: str
    tool_refs: list[str] = Field(default_factory=list)
    tool_bindings: list[AgentToolBinding] = Field(default_factory=list)
    # Cap on tool executions per agent step (None = runtime default). When
    # the model requests calls beyond the cap, the runtime forces a final
    # answer instead of executing them.
    max_tool_calls: int | None = Field(default=None, ge=0)
    memory_refs: list[str] = Field(default_factory=list)
    retry_policy: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int | None = Field(default=None, ge=1)
    state_persistence: dict[str, Any] = Field(default_factory=dict)
    thread_participation: Literal["none", "read", "write", "full"] = "none"
    model_params: dict[str, Any] = Field(default_factory=dict)
    mcp_servers: list[dict[str, Any]] = Field(default_factory=list)
    template_ref: TemplateReference | None = None
    context_window: ContextWindowSettings | None = None
    template_memory_bindings: list[TemplateMemoryBinding] = Field(default_factory=list)
    # When set, the input payload field under this key is read as a list of
    # chat messages ({role: human|ai|tool, content}) and rendered as real
    # conversation turns instead of being dumped inside the input JSON block.
    input_messages_key: str | None = None
    # Persist the conversation in thread state: stored turns are replayed
    # before the incoming ones, and each successful run appends the new turns
    # plus the agent's reply. Requires input_messages_key; runs continue a
    # conversation by submitting the same thread_id.
    persist_conversation: bool = False
    # Cap on conversation turns kept (and replayed) when persisting.
    # None keeps everything.
    conversation_max_turns: int | None = Field(default=None, ge=1)
    # Stakes tier for this node. Gates cost automation: the cheap-first cascade below is
    # only ever activated on "low" nodes, because it escalates on hard failure but cannot
    # judge subtle quality loss. "medium"/"high" nodes stay advise-only.
    criticality: Literal["low", "medium", "high"] = "medium"
    # Cost cascade (opt-in, off by default): when enabled, try `cheap_model` first and
    # escalate to `model_provider` (the incumbent) only on a hard failure (provider error or
    # blank response). A human enables this per node; the runtime additionally refuses to
    # cascade unless `criticality == "low"`. `cheap_model` is a right-sizing candidate ref.
    cascade_enabled: bool = False
    cheap_model: str | None = None


class ExecutableUnitNodeData(BaseModel):
    """Configuration for a code/script execution step.

    Two authoring paths share this node: a ``manifest_ref`` pointing at a
    registered unit (the medium-code path), or ``inline_source`` carrying
    authored code directly (the Studio code node). Exactly one must be set;
    inline code always runs as a sandboxed subprocess.
    """

    manifest_ref: str = ""
    execution_mode: Literal["native", "wrapped_command", "project", "inline"]
    inline_source: str | None = None
    timeout_seconds: int | None = Field(default=None, gt=0)
    runtime_binding: str | None = None
    sandbox_config: dict[str, Any] = Field(default_factory=dict)
    output_extraction_strategy: str = "json_stdout"

    @model_validator(mode="after")
    def _validate_code_source(self) -> ExecutableUnitNodeData:
        """Require exactly one of manifest_ref / inline_source, modes aligned."""
        has_ref = bool(self.manifest_ref.strip())
        has_inline = self.inline_source is not None
        if has_ref and has_inline:
            raise ValueError("manifest_ref and inline_source are mutually exclusive")
        if has_inline and self.execution_mode != "inline":
            raise ValueError("inline_source requires execution_mode='inline'")
        if not has_inline and self.execution_mode == "inline":
            raise ValueError("execution_mode='inline' requires inline_source")
        return self


class HumanApprovalNodeData(BaseModel):
    """Configuration for a step that pauses and waits for a human to approve.

    Defines what data the approver sees and how they can respond.
    """

    approval_payload_schema_ref: str | None = None
    resolution_schema_ref: str | None = None
    approval_policy_config: dict[str, Any] = Field(default_factory=dict)
    pause_behavior_config: dict[str, Any] = Field(default_factory=dict)
    sla_timeout_seconds: int | None = None
    escalation_action: str | None = None
    delegate_identity: dict[str, Any] | None = None


class RetrievalNodeData(BaseModel):
    """Configuration for a retrieval (RAG) step.

    Queries a vector memory connector with text taken from the node input and
    outputs the retrieved chunks for a downstream node (typically an agent) to
    ground on. Embedding and ranking are owned by the connector.
    """

    connector_ref: str
    query_key: str = "query"
    top_k: int = Field(default=5, ge=1)
    scope: Literal["run", "thread", "shared"] = "shared"
    as_name: str = "retrieved"


class EntrypointNodeData(BaseModel):
    """Configuration for the workflow's entrypoint. Deliberately empty.

    The entrypoint's contract lives on the node's shared ``input_contract_ref``
    — it is the workflow's public input contract, pinned into the deployment
    snapshot and enforced against every submitted run payload.
    """


class EntrypointNode(NodeBase):
    """The node where a run enters the graph.

    Declares the workflow's public input contract and passes the (already
    ingress-validated) payload through unchanged, leaving an audit record of
    what entered the workflow.
    """

    node_type: Literal["entrypoint"] = "entrypoint"
    entrypoint: EntrypointNodeData = Field(default_factory=EntrypointNodeData)

    def to_governed_step_spec(self) -> GovernedStepSpec:
        """Convert this entrypoint into a spec the execution engine understands."""
        return GovernedStepSpec(
            name=self.node_id,
            tool={
                "kind": "entrypoint_ref",
                "input_contract_ref": self.input_contract_ref,
                "output_contract_ref": self.output_contract_ref,
            },
        )


class AgentNode(NodeBase):
    """A graph node that runs an AI agent.

    Wraps an AgentNodeData with the shared node fields like contracts
    and policy bindings.
    """

    node_type: Literal["agent"] = "agent"
    agent: AgentNodeData

    def to_governed_step_spec(self) -> GovernedStepSpec:
        """Convert this agent node into a spec the execution engine understands."""
        return GovernedStepSpec(
            name=self.node_id,
            agent={
                "kind": "agent_ref",
                "provider_ref": self.agent.model_provider,
                "instruction_ref": self.agent.instruction,
                "tool_refs": list(self.agent.tool_refs),
                "tool_bindings": [binding.model_dump() for binding in self.agent.tool_bindings],
                "memory_refs": list(self.agent.memory_refs),
                "input_contract_ref": self.input_contract_ref,
                "output_contract_ref": self.output_contract_ref,
                "policy_refs": list(self.policy_bindings),
                "capability_refs": list(self.capability_bindings),
            },
        )


class ExecutableUnitNode(NodeBase):
    """A graph node that runs a code or script executable unit.

    Wraps an ExecutableUnitNodeData with the shared node fields.
    """

    node_type: Literal["executable_unit"] = "executable_unit"
    executable_unit: ExecutableUnitNodeData

    def to_governed_step_spec(self) -> GovernedStepSpec:
        """Convert this executable unit node into a spec the execution engine understands."""
        return GovernedStepSpec(
            name=self.node_id,
            tool={
                "kind": "executable_unit_ref",
                "manifest_ref": self.executable_unit.manifest_ref,
                "execution_mode": self.executable_unit.execution_mode,
                "runtime_binding": self.executable_unit.runtime_binding,
                "sandbox_config": dict(self.executable_unit.sandbox_config),
                "output_extraction_strategy": self.executable_unit.output_extraction_strategy,
                "input_contract_ref": self.input_contract_ref,
                "output_contract_ref": self.output_contract_ref,
                "policy_refs": list(self.policy_bindings),
                "capability_refs": list(self.capability_bindings),
            },
        )


class HumanApprovalNode(NodeBase):
    """A graph node that pauses execution until a human approves.

    Wraps a HumanApprovalNodeData with the shared node fields.
    """

    node_type: Literal["human_approval"] = "human_approval"
    human_approval: HumanApprovalNodeData

    def to_governed_step_spec(self) -> GovernedStepSpec:
        """Convert this approval node into a spec the execution engine understands."""
        return GovernedStepSpec(
            name=self.node_id,
            agent={
                "kind": "human_approval_ref",
                "approval_payload_schema_ref": self.human_approval.approval_payload_schema_ref,
                "resolution_schema_ref": self.human_approval.resolution_schema_ref,
                "approval_policy_refs": list(self.policy_bindings),
                "pause_behavior_config": dict(self.human_approval.pause_behavior_config),
                "approval_policy_config": dict(self.human_approval.approval_policy_config),
            },
            approval_override=True,
        )


class SubgraphNode(NodeBase):
    """A graph node that invokes another published graph as a child workflow.

    Wraps a SubgraphNodeData with the shared node fields.  The child
    graph is resolved at execution time via the SubgraphResolver.
    """

    node_type: Literal["subgraph"] = "subgraph"
    subgraph: SubgraphNodeData

    def to_governed_step_spec(self) -> GovernedStepSpec:
        """Convert this subgraph node into a spec the execution engine understands."""
        return GovernedStepSpec(
            name=self.node_id,
            agent={
                "kind": "subgraph_ref",
                "graph_ref": self.subgraph.graph_ref,
                "version": self.subgraph.version,
            },
        )


class RetrievalNode(NodeBase):
    """A graph node that retrieves grounded context from a vector memory connector."""

    node_type: Literal["retrieval"] = "retrieval"
    retrieval: RetrievalNodeData

    def to_governed_step_spec(self) -> GovernedStepSpec:
        """Convert this retrieval node into a spec the execution engine understands."""
        return GovernedStepSpec(
            name=self.node_id,
            tool={
                "kind": "retrieval_ref",
                "connector_ref": self.retrieval.connector_ref,
                "top_k": self.retrieval.top_k,
                "scope": self.retrieval.scope,
                "input_contract_ref": self.input_contract_ref,
                "output_contract_ref": self.output_contract_ref,
                "policy_refs": list(self.policy_bindings),
                "capability_refs": list(self.capability_bindings),
            },
        )


Node = Annotated[
    EntrypointNode
    | AgentNode
    | ExecutableUnitNode
    | HumanApprovalNode
    | SubgraphNode
    | RetrievalNode,
    Field(discriminator="node_type"),
]


class Edge(BaseModel):
    """A connection between two nodes in the graph.

    Data edges define the flow of execution.  They can optionally carry a
    condition (to branch) and a mapping (to transform data between nodes).
    Tool edges (``kind="tool"``) attach an executable unit to an agent as a
    callable tool — they are structural, never traversed as control flow.
    """

    model_config = ConfigDict(extra="forbid")

    edge_id: str
    source_node_id: str
    target_node_id: str
    kind: Literal["data", "tool"] = "data"
    mapping: EdgeMapping | None = None
    condition: Condition | None = None
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class Graph(BaseModel):
    """The top-level object representing an entire workflow graph.

    A graph contains nodes (the steps), edges (the connections), execution
    settings, and metadata.  It also tracks its lifecycle status (draft,
    published, or archived) and version number.
    """

    model_config = ConfigDict(extra="forbid")

    graph_id: str
    name: str
    version: int = Field(default=1, ge=1)
    status: GraphStatus = GraphStatus.DRAFT
    # WS-B: tenant that owns this graph. Persisted BOTH inside the serialized
    # payload (round-trips here) and as a dedicated ``graph_versions.tenant_id``
    # column so the repository can filter by it. Defaults to the reserved
    # single-tenant sentinel so backfilled/code-authored graphs stay readable.
    tenant_id: str = "default"
    workspace_id: str | None = None
    entry_step: str | None = None
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    execution_settings: ExecutionSettings = Field(default_factory=ExecutionSettings)
    policy_bindings: list[str] = Field(default_factory=list)
    deployment_settings: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _validate_references(self) -> Graph:
        node_ids = {node.node_id for node in self.nodes}
        if self.entry_step is not None and self.entry_step not in node_ids:
            msg = f"entry step references unknown node: {self.entry_step}"
            raise ValueError(msg)
        missing_edges = [
            edge.edge_id
            for edge in self.edges
            if edge.source_node_id not in node_ids or edge.target_node_id not in node_ids
        ]
        if missing_edges:
            msg = f"edges reference unknown nodes: {', '.join(missing_edges)}"
            raise ValueError(msg)
        return self

    def transition_to(self, status: GraphStatus) -> Graph:
        """Move the graph to a new lifecycle status (e.g. draft -> published).

        Returns a new Graph object with the updated status.
        Raises ValueError if the transition is not allowed.
        """
        allowed_transitions: dict[GraphStatus, set[GraphStatus]] = {
            GraphStatus.DRAFT: {GraphStatus.PUBLISHED, GraphStatus.ARCHIVED},
            GraphStatus.PUBLISHED: {GraphStatus.ARCHIVED},
            GraphStatus.ARCHIVED: set(),
        }
        if status == self.status:
            return self.model_copy(update={"updated_at": utc_now()})
        if status not in allowed_transitions[self.status]:
            msg = f"invalid graph status transition: {self.status.value} -> {status.value}"
            raise ValueError(msg)
        return self.model_copy(update={"status": status, "updated_at": utc_now()})

    def publish(self) -> Graph:
        """Mark this graph as published (ready to run)."""
        return self.transition_to(GraphStatus.PUBLISHED)

    def archive(self) -> Graph:
        """Mark this graph as archived (no longer active)."""
        return self.transition_to(GraphStatus.ARCHIVED)

    def to_governed_flow_spec(self) -> GovernedFlowSpec:
        """Convert the entire graph into a GovernedFlowSpec for the execution engine.

        This compiles nodes into steps and edges into transitions, producing
        the format the runtime expects.
        """
        steps = [node.to_governed_step_spec() for node in self.nodes]
        if self.entry_step is None:
            entry_step = steps[0].name if steps else None
        else:
            entry_step = self.entry_step

        transitions = self._transitions_by_source()
        compiled_steps: list[GovernedStepSpec] = []
        for step in steps:
            transition = transitions.get(step.name)
            compiled_steps.append(
                GovernedStepSpec(
                    name=step.name,
                    tool=getattr(step, "tool", None),
                    agent=getattr(step, "agent", None),
                    required_artifacts=list(getattr(step, "required_artifacts", [])),
                    emitted_artifact=getattr(step, "emitted_artifact", None),
                    approval_override=getattr(step, "approval_override", None),
                    transition=transition,
                )
            )

        return GovernedFlowSpec(
            name=self.name,
            steps=compiled_steps,
            entry_step=entry_step,
            policies=[{"ref": policy_ref} for policy_ref in self.policy_bindings],
        )

    def _transitions_by_source(self) -> dict[str, TransitionSpec]:
        """Build a mapping from each node to its outgoing transition spec."""
        outgoing: dict[str, list[Edge]] = {}
        for edge in self.edges:
            # Tool edges attach tools; they are not control-flow transitions.
            if not edge.enabled or edge.kind == "tool":
                continue
            outgoing.setdefault(edge.source_node_id, []).append(edge)

        transitions: dict[str, TransitionSpec] = {}
        for node in self.nodes:
            edges = outgoing.get(node.node_id, [])
            transitions[node.node_id] = self._compile_transition(node.node_id, edges)
        return transitions

    def _compile_transition(self, node_id: str, edges: list[Edge]) -> TransitionSpec:
        """Turn a list of outgoing edges into a single transition spec."""
        if not edges:
            return end()
        if len(edges) == 1:
            return then(edges[0].target_node_id)

        conditional_edges = [edge for edge in edges if edge.condition is not None]
        if conditional_edges:
            mapping = {
                edge.condition.expression if edge.condition else edge.edge_id: edge.target_node_id
                for edge in edges
            }
            return branch(router=f"{node_id}_router", mapping=mapping)

        allowed = [edge.target_node_id for edge in edges]
        return route_to(allowed=allowed)
