"""Core data models that define what a graph looks like.

A graph is made up of nodes (the steps) and edges (the connections between
steps).  This module contains all the Pydantic models that represent those
pieces, plus helper enums and settings objects.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from governai.app.spec import (
    GovernedFlowSpec,
    GovernedStepSpec,
    TransitionSpec,
    branch,
    end,
    route_to,
    then,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

from zeroth.core.context_window.models import ContextWindowSettings
from zeroth.core.mappings.models import EdgeMapping
from zeroth.core.parallel.models import ParallelConfig
from zeroth.core.subgraph.models import SubgraphNodeData
from zeroth.core.templates.models import TemplateReference


def _utc_now() -> datetime:
    """Return the current time in UTC."""
    return datetime.now(UTC)


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

    def to_governed_step_spec(self) -> GovernedStepSpec:
        """Convert this node into a GovernedStepSpec for the execution engine."""
        raise NotImplementedError


class AgentNodeData(BaseModel):
    """Configuration for an AI agent step.

    Holds the instruction prompt, which model to use, what tools and memory
    the agent can access, and other agent-specific settings.
    """

    instruction: str
    model_provider: str
    tool_refs: list[str] = Field(default_factory=list)
    memory_refs: list[str] = Field(default_factory=list)
    retry_policy: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int | None = Field(default=None, ge=1)
    state_persistence: dict[str, Any] = Field(default_factory=dict)
    thread_participation: Literal["none", "read", "write", "full"] = "none"
    model_params: dict[str, Any] = Field(default_factory=dict)
    mcp_servers: list[dict[str, Any]] = Field(default_factory=list)
    template_ref: TemplateReference | None = None
    context_window: ContextWindowSettings | None = None


class ExecutableUnitNodeData(BaseModel):
    """Configuration for a code/script execution step.

    Points to a manifest that describes what to run, how to run it,
    and how to extract the output.
    """

    manifest_ref: str
    execution_mode: Literal["native", "wrapped_command", "project"]
    runtime_binding: str | None = None
    sandbox_config: dict[str, Any] = Field(default_factory=dict)
    output_extraction_strategy: str = "json_stdout"


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


Node = Annotated[
    AgentNode | ExecutableUnitNode | HumanApprovalNode | SubgraphNode,
    Field(discriminator="node_type"),
]


class Edge(BaseModel):
    """A connection between two nodes in the graph.

    Edges define the flow of execution.  They can optionally carry a
    condition (to branch) and a mapping (to transform data between nodes).
    """

    model_config = ConfigDict(extra="forbid")

    edge_id: str
    source_node_id: str
    target_node_id: str
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
    entry_step: str | None = None
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    execution_settings: ExecutionSettings = Field(default_factory=ExecutionSettings)
    policy_bindings: list[str] = Field(default_factory=list)
    deployment_settings: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)

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
            return self.model_copy(update={"updated_at": _utc_now()})
        if status not in allowed_transitions[self.status]:
            msg = f"invalid graph status transition: {self.status.value} -> {status.value}"
            raise ValueError(msg)
        return self.model_copy(update={"status": status, "updated_at": _utc_now()})

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
            if not edge.enabled:
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
