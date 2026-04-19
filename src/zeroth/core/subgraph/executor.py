"""Subgraph executor -- creates child Runs and recursively drives subgraphs.

The ``SubgraphExecutor`` is the core engine for subgraph composition.
It resolves a published graph by reference, namespaces its node IDs,
merges parent governance policies, creates a child Run, and recursively
calls the orchestrator's ``_drive()`` loop to execute the subgraph.

Depth tracking and cycle detection prevent infinite recursion.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from zeroth.core.graph.models import Graph, SubgraphNode
from zeroth.core.parallel.models import BranchContext, GlobalStepTracker
from zeroth.core.runs.models import Run, RunStatus
from zeroth.core.subgraph.errors import (
    SubgraphCycleError,
    SubgraphDepthLimitError,
    SubgraphExecutionError,
)
from zeroth.core.subgraph.resolver import (
    SubgraphResolver,
    merge_governance,
    namespace_subgraph,
)

if TYPE_CHECKING:
    from zeroth.core.orchestrator.runtime import RuntimeOrchestrator

logger = logging.getLogger(__name__)


def _sum_audit_cost(history: list) -> float:
    """Aggregate per-step cost from a Run's execution history (W-4).

    Walks the execution history records and sums any ``cost_usd`` field
    present on each entry. Used by ``SubgraphExecutor.execute`` to write
    a single ``total_cost_usd`` key on the child Run's metadata before
    returning — the ONLY place this key is written (D-09). ``_drive``
    stays cost-agnostic to avoid polluting every non-parallel run with
    parallel-specific behavior.
    """
    total = 0.0
    for entry in history or []:
        cost: Any = None
        if isinstance(entry, dict):
            cost = entry.get("cost_usd")
        else:
            cost = getattr(entry, "cost_usd", None)
        if cost:
            with contextlib.suppress(TypeError, ValueError):
                total += float(cost)
    return total


@dataclass(slots=True)
class SubgraphExecutor:
    """Executes subgraph nodes by creating child Runs and recursively driving them.

    Given a ``SubgraphNode``, the executor:

    1. Checks depth limits and cycle detection before any work.
    2. Resolves the subgraph graph_ref via the configured resolver.
    3. Namespaces child node/edge IDs to prevent collisions.
    4. Merges parent governance policies (parent-ceiling model).
    5. Creates a child Run with correct parent linkage and thread participation.
    6. Recursively calls ``orchestrator._drive()`` on the merged subgraph.
    """

    resolver: SubgraphResolver

    async def execute(
        self,
        orchestrator: RuntimeOrchestrator,
        parent_graph: Graph,
        parent_run: Run,
        node: SubgraphNode,
        node_id: str,
        input_payload: dict[str, Any],
        *,
        branch_context: BranchContext | None = None,
        step_tracker: GlobalStepTracker | None = None,
    ) -> Run:
        """Execute a subgraph node by creating a child Run and driving it.

        Parameters
        ----------
        orchestrator:
            The RuntimeOrchestrator instance to use for recursive _drive().
        parent_graph:
            The parent graph containing this SubgraphNode.
        parent_run:
            The parent Run that is currently being driven.
        node:
            The SubgraphNode to execute.
        node_id:
            The node ID of the SubgraphNode in the parent graph.
        input_payload:
            The input data for this subgraph invocation.

        Returns:
        -------
        Run:
            The child Run after it has been driven to completion (or failure).

        Raises:
        ------
        SubgraphDepthLimitError:
            If the nesting depth exceeds the configured max_depth.
        SubgraphCycleError:
            If a circular subgraph reference is detected.
        SubgraphExecutionError:
            If the orchestrator is None or if _drive() raises unexpectedly.
        """
        if orchestrator is None:
            raise SubgraphExecutionError("orchestrator is required to execute subgraph nodes")

        subgraph_data = node.subgraph
        graph_ref = subgraph_data.graph_ref

        # --- Depth check (T-39-06 mitigation) ---
        current_depth = parent_run.metadata.get("subgraph_depth", 0)
        new_depth = current_depth + 1
        if new_depth > subgraph_data.max_depth:
            raise SubgraphDepthLimitError(
                f"subgraph depth {new_depth} exceeds max_depth "
                f"{subgraph_data.max_depth} for graph_ref '{graph_ref}'"
            )

        # --- Cycle detection ---
        visited_refs: list[str] = list(parent_run.metadata.get("visited_subgraph_refs", []))
        if graph_ref in visited_refs:
            raise SubgraphCycleError(f"circular subgraph reference detected: {graph_ref}")

        # --- Resolve child graph ---
        subgraph, deployment = await self.resolver.resolve(graph_ref, subgraph_data.version)

        # --- Namespace node IDs (T-39-09 + D-10) ---
        namespaced = namespace_subgraph(
            subgraph,
            graph_ref,
            new_depth,
            branch_index=(
                branch_context.branch_index if branch_context is not None else None
            ),
        )

        # --- Merge governance (parent-ceiling) ---
        merged = merge_governance(parent_graph, namespaced)

        # --- Determine thread_id (D-06: force isolated inside parallel branch) ---
        if branch_context is not None:
            # D-06: regardless of SubgraphNodeData.thread_participation,
            # subgraphs invoked from a parallel branch always get a fresh
            # thread so concurrent branches cannot corrupt a shared thread.
            logger.debug(
                "forcing isolated thread for subgraph %s inside branch %d",
                graph_ref,
                branch_context.branch_index,
            )
            child_thread_id = ""
        elif subgraph_data.thread_participation == "inherit":
            child_thread_id = parent_run.thread_id
        else:
            # "isolated" -- empty string triggers auto-generation via Run model validator
            child_thread_id = ""

        # --- Build child Run metadata ---
        child_visited = visited_refs + [graph_ref]
        entry = orchestrator._entry_step(merged)
        child_metadata: dict[str, Any] = {
            "subgraph_depth": new_depth,
            "parent_run_id": parent_run.run_id,
            "parent_node_id": node_id,
            "visited_subgraph_refs": child_visited,
            "node_payloads": {entry: dict(input_payload)},
            "graph_id": merged.graph_id,
            "graph_name": merged.name,
            "edge_visit_counts": {},
            "path": [],
            "audits": {},
        }

        # --- Create child Run (T-39-07 mitigation: inherit tenant/workspace) ---
        child_run = Run(
            graph_version_ref=f"{subgraph.graph_id}:v{subgraph.version}",
            deployment_ref=graph_ref,
            thread_id=child_thread_id,
            tenant_id=parent_run.tenant_id,
            workspace_id=parent_run.workspace_id,
            parent_run_id=parent_run.run_id,
            pending_node_ids=[entry],
            metadata=child_metadata,
        )
        child_run = await orchestrator.run_repository.create(child_run)
        child_run.status = RunStatus.RUNNING
        child_run.touch()
        child_run = await orchestrator.run_repository.put(child_run)
        await orchestrator.run_repository.write_checkpoint(child_run)

        # --- Recursive execution (D-08/D-12: share parent's step_tracker) ---
        try:
            result = await orchestrator._drive(merged, child_run, step_tracker=step_tracker)
        except Exception as exc:
            raise SubgraphExecutionError(f"subgraph '{graph_ref}' execution failed: {exc}") from exc

        # --- D-09 / W-4: cost rollup at child-return path ONLY ---
        # This is the sole writer of `total_cost_usd` on a Run's metadata.
        # `_drive()` stays cost-agnostic; `BranchResult.cost_usd` reads
        # this field downstream via `_sum_run_cost`.
        result.metadata["total_cost_usd"] = _sum_audit_cost(result.execution_history)

        return result

    async def resume(
        self,
        orchestrator: RuntimeOrchestrator,
        parent_graph: Graph,
        parent_run: Run,
        paused_child_run_id: str,
        *,
        branch_index: int | None = None,
        step_tracker: GlobalStepTracker | None = None,
    ) -> Run:
        """Resume a paused child subgraph Run in place (D-11 literal).

        Used by ``_execute_parallel_fan_out_resume`` to re-drive ONLY
        the branch that hit WAITING_APPROVAL. Re-namespaces the child
        graph using the SAME ``branch_index`` as the original
        execution so audit IDs remain byte-identical (T-43-01
        mitigation).
        """
        child_run = await orchestrator.run_repository.get(paused_child_run_id)
        if child_run is None:
            raise SubgraphExecutionError(
                f"paused child run {paused_child_run_id} not found"
            )

        graph_ref = child_run.deployment_ref
        depth = child_run.metadata.get("subgraph_depth", 1)

        subgraph, _ = await self.resolver.resolve(graph_ref, None)
        namespaced = namespace_subgraph(
            subgraph, graph_ref, depth, branch_index=branch_index
        )
        merged = merge_governance(parent_graph, namespaced)

        try:
            result = await orchestrator._drive(
                merged, child_run, step_tracker=step_tracker
            )
        except Exception as exc:
            raise SubgraphExecutionError(
                f"subgraph resume for '{graph_ref}' failed: {exc}"
            ) from exc

        result.metadata["total_cost_usd"] = _sum_audit_cost(result.execution_history)
        return result
