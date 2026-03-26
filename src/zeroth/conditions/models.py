"""Data models used throughout the conditions subsystem.

These Pydantic models represent the key data structures for condition evaluation:
bindings, contexts, outcomes, branch resolutions, and next-step plans. They are
the "nouns" of the conditions system -- the things that get created, passed
around, and stored.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from zeroth.graph.models import Condition as GraphCondition
from zeroth.runs.models import RunConditionResult


def _utc_now() -> datetime:
    """Return the current time in UTC. Used as a default factory for timestamps."""
    return datetime.now(UTC)


class ConditionOutcome(BaseModel):
    """The full result of evaluating a condition, including where it came from.

    Wraps a RunConditionResult with extra info about which graph, edge, and
    nodes were involved. Useful for debugging and auditing why a particular
    branch was taken.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    result: RunConditionResult
    graph_id: str
    source_node_id: str
    target_node_id: str | None = None
    edge_id: str | None = None
    expression: str | None = None
    branch_rule: str | None = None
    operand_refs: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class ConditionBinding(BaseModel):
    """Links a graph edge to its condition so it can be evaluated at runtime.

    Contains everything the evaluator needs: which graph and edge this came
    from, the source and target nodes, the condition to check, and whether
    the edge is enabled.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    graph_id: str
    edge_id: str
    source_node_id: str
    target_node_id: str
    condition: GraphCondition | None = None
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConditionContext(BaseModel):
    """Holds all the runtime data that conditions can reference during evaluation.

    Think of this as the "world state" that condition expressions can read from.
    It includes the current payload, workflow state, user-defined variables,
    visit counts, and the traversal path so far.
    """

    model_config = ConfigDict(extra="forbid")

    payload: dict[str, Any] = Field(default_factory=dict)
    state: dict[str, Any] = Field(default_factory=dict)
    variables: dict[str, Any] = Field(default_factory=dict)
    node_visit_counts: dict[str, int] = Field(default_factory=dict)
    edge_visit_counts: dict[str, int] = Field(default_factory=dict)
    path: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def namespace(self) -> dict[str, Any]:
        """Build the dictionary of names that condition expressions can access.

        For example, an expression like "payload.score > 10" will look up
        "payload" in this namespace to find the actual payload dict.
        """
        return {
            "payload": self.payload,
            "state": self.state,
            "variables": self.variables,
            "node_visit_counts": self.node_visit_counts,
            "edge_visit_counts": self.edge_visit_counts,
            "path": list(self.path),
            "metadata": self.metadata,
        }


class TraversalState(BaseModel):
    """Tracks how many times each node and edge has been visited during a run.

    The branch resolver uses this to enforce visit limits and prevent infinite
    loops. The 'path' list records the order nodes were visited.
    """

    model_config = ConfigDict(extra="forbid")

    node_visit_counts: dict[str, int] = Field(default_factory=dict)
    edge_visit_counts: dict[str, int] = Field(default_factory=dict)
    path: list[str] = Field(default_factory=list)


class BranchResolution(BaseModel):
    """The result of figuring out which outgoing edges from a node are active.

    After evaluation, this tells you which edges passed their conditions
    (active), which did not (suppressed), and which nodes to visit next.
    If no edges are active, terminal_reason explains why execution stopped.
    """

    model_config = ConfigDict(extra="forbid")

    graph_id: str
    source_node_id: str
    active_edge_ids: list[str] = Field(default_factory=list)
    suppressed_edge_ids: list[str] = Field(default_factory=list)
    next_node_ids: list[str] = Field(default_factory=list)
    condition_results: list[RunConditionResult] = Field(default_factory=list)
    terminal_reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    resolved_at: datetime = Field(default_factory=_utc_now)


class NextStepPlan(BaseModel):
    """Describes what the workflow should do next after the current node.

    Contains the list of next node IDs to execute, the full branch resolution
    details, and the current traversal state. If there are no next nodes,
    terminal_reason explains why execution is finished.
    """

    model_config = ConfigDict(extra="forbid")

    graph_id: str
    current_node_id: str
    next_node_ids: list[str] = Field(default_factory=list)
    branch_resolution: BranchResolution
    traversal_state: TraversalState = Field(default_factory=TraversalState)
    terminal_reason: str | None = None
