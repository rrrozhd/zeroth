"""Branch resolution and next-step planning for workflow graphs.

Given a node in a workflow graph, this module figures out which outgoing edges
are active (their conditions passed), which are suppressed, and what the next
step(s) should be. It also enforces visit limits to prevent infinite loops.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from zeroth.conditions.binding import ConditionBinder
from zeroth.conditions.evaluator import ConditionEvaluator
from zeroth.conditions.models import (
    BranchResolution,
    ConditionBinding,
    ConditionContext,
    NextStepPlan,
    TraversalState,
)
from zeroth.graph.models import Graph
from zeroth.runs.models import RunConditionResult


class BranchResolver:
    """Figures out which outgoing edges from a node should be followed.

    For a given node, it looks at all outgoing edges, evaluates their
    conditions, checks visit limits, and returns a BranchResolution that
    says which edges are active and which nodes to visit next.
    """

    def __init__(
        self,
        *,
        binder: ConditionBinder | None = None,
        evaluator: ConditionEvaluator | None = None,
    ) -> None:
        self._binder = binder or ConditionBinder()
        self._evaluator = evaluator or ConditionEvaluator()

    def resolve(
        self,
        graph: Graph,
        source_node_id: str,
        context: ConditionContext | Mapping[str, Any] | None = None,
        *,
        traversal_state: TraversalState | None = None,
    ) -> BranchResolution:
        """Evaluate all outgoing edges from a node and return which ones are active.

        Edges can be suppressed because they are disabled, their condition
        evaluated to false, or a visit limit would be exceeded.
        """
        runtime_context = self._as_context(context)
        traversal_state = traversal_state or self._traversal_state(runtime_context)
        # Filter to only edges leaving this node (ignore edges from other nodes)
        bindings = [
            binding
            for binding in self._binder.bind_graph(graph)
            if binding.source_node_id == source_node_id
        ]
        # No outgoing edges means this node is a terminal (end) node
        if not bindings:
            return BranchResolution(
                graph_id=graph.graph_id,
                source_node_id=source_node_id,
                terminal_reason="no_outgoing_edges",
            )

        active_edge_ids: list[str] = []
        suppressed_edge_ids: list[str] = []
        next_node_ids: list[str] = []
        condition_results = []

        for binding in bindings:
            active, result, reason = self._evaluate_binding(
                graph,
                binding,
                runtime_context,
                traversal_state,
            )
            condition_results.append(result)
            if not active:
                suppressed_edge_ids.append(binding.edge_id)
                continue
            active_edge_ids.append(binding.edge_id)
            next_node_ids.append(binding.target_node_id)
            if reason is not None:
                condition_results[-1].details["suppression_reason"] = reason

        terminal_reason = None if next_node_ids else "branch_suppressed"
        return BranchResolution(
            graph_id=graph.graph_id,
            source_node_id=source_node_id,
            active_edge_ids=active_edge_ids,
            suppressed_edge_ids=suppressed_edge_ids,
            next_node_ids=next_node_ids,
            condition_results=condition_results,
            terminal_reason=terminal_reason,
        )

    def _evaluate_binding(
        self,
        graph: Graph,
        binding: ConditionBinding,
        context: ConditionContext,
        traversal_state: TraversalState,
    ) -> tuple[bool, RunConditionResult, str | None]:
        """Check a single binding and return (is_active, result, suppression_reason)."""
        if not binding.enabled:
            return (
                False,
                self._build_result(binding, matched=False, reason="edge_disabled"),
                "edge_disabled",
            )

        if binding.condition is None:
            return True, self._build_result(binding, matched=True, value=True), None

        result = self._evaluator.evaluate(
            binding.condition,
            context,
            condition_id=binding.edge_id,
            edge_id=binding.edge_id,
            source_node_id=binding.source_node_id,
            target_node_id=binding.target_node_id,
            metadata=binding.metadata,
        )

        if not result.matched:
            return False, result, "condition_false"
        if self._would_exceed_visit_limits(graph, binding, traversal_state):
            result.details["suppression_reason"] = "visit_limit"
            return False, result, "visit_limit"
        return True, result, None

    def _would_exceed_visit_limits(
        self,
        graph: Graph,
        binding: ConditionBinding,
        traversal_state: TraversalState,
    ) -> bool:
        """Return True if following this edge would break a visit limit or create a cycle."""
        max_visits_per_node = graph.execution_settings.max_visits_per_node
        max_visits_per_edge = graph.execution_settings.max_visits_per_edge
        next_node_visits = traversal_state.node_visit_counts.get(binding.target_node_id, 0) + 1
        next_edge_visits = traversal_state.edge_visit_counts.get(binding.edge_id, 0) + 1
        return (
            (max_visits_per_node is not None and next_node_visits > max_visits_per_node)
            or (max_visits_per_edge is not None and next_edge_visits > max_visits_per_edge)
            or (
                binding.condition is not None
                and not binding.condition.allow_cycle_traversal
                and binding.target_node_id in traversal_state.path
            )
        )

    def _build_result(
        self,
        binding: ConditionBinding,
        *,
        matched: bool,
        value: Any = None,
        reason: str | None = None,
    ) -> RunConditionResult:
        """Create a RunConditionResult for a binding with all the debugging details filled in."""
        branch_rule = (
            binding.condition.branch_rule if binding.condition is not None else "passthrough"
        )
        operand_refs = (
            list(binding.condition.operand_refs) if binding.condition is not None else []
        )
        expression = binding.condition.expression if binding.condition is not None else None
        details = {
            "expression": expression,
            "branch_rule": branch_rule,
            "operand_refs": operand_refs,
            "value": value,
            "graph_context": dict(binding.metadata),
            "source_node_id": binding.source_node_id,
            "target_node_id": binding.target_node_id,
            "edge_id": binding.edge_id,
        }
        if reason is not None:
            details["suppression_reason"] = reason
        return RunConditionResult(
            condition_id=binding.edge_id,
            selected_edge_id=binding.edge_id if matched else None,
            matched=matched,
            details=details,
        )

    def _traversal_state(self, context: ConditionContext) -> TraversalState:
        """Build a TraversalState from the visit counts and path in the context."""
        return TraversalState(
            node_visit_counts=dict(context.node_visit_counts),
            edge_visit_counts=dict(context.edge_visit_counts),
            path=list(context.path),
        )

    def _as_context(self, context: ConditionContext | Mapping[str, Any] | None) -> ConditionContext:
        """Ensure the context is a ConditionContext, converting from a dict or None if needed."""
        if context is None:
            return ConditionContext()
        if isinstance(context, ConditionContext):
            return context
        return ConditionContext.model_validate(dict(context))
class NextStepPlanner:
    """Determines which node(s) to execute next in a workflow.

    This is a higher-level wrapper around BranchResolver. Given the current
    node, it resolves branches and produces a NextStepPlan that the runtime
    can use to decide what to do next.
    """

    def __init__(self, resolver: BranchResolver | None = None) -> None:
        self._resolver = resolver or BranchResolver()

    def plan(
        self,
        graph: Graph,
        current_node_id: str,
        context: ConditionContext | Mapping[str, Any] | None = None,
        *,
        traversal_state: TraversalState | None = None,
    ) -> NextStepPlan:
        """Resolve branches from the current node and return a plan for what to do next."""
        resolution = self._resolver.resolve(
            graph,
            current_node_id,
            context,
            traversal_state=traversal_state,
        )
        runtime_context = self._resolver._as_context(context)
        if traversal_state is None:
            traversal_state = self._resolver._traversal_state(runtime_context)
        return NextStepPlan(
            graph_id=graph.graph_id,
            current_node_id=current_node_id,
            next_node_ids=list(resolution.next_node_ids),
            branch_resolution=resolution,
            traversal_state=traversal_state,
            terminal_reason=resolution.terminal_reason,
        )
