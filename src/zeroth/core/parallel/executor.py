"""Parallel fan-out/fan-in execution engine.

The ParallelExecutor handles three phases of parallel execution:
1. Fan-out: splits a node's output list into N isolated BranchContexts
2. Execute: runs all branches concurrently via asyncio.gather
3. Fan-in: collects branch results into a deterministically ordered aggregate
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

from zeroth.core.mappings.executor import _get_path, _set_path
from zeroth.core.parallel.errors import (
    FanOutValidationError,
    ParallelExecutionError,
)
from zeroth.core.parallel.models import (
    BranchContext,
    BranchResult,
    FanInResult,
    ParallelConfig,
)
from zeroth.core.parallel.reducers import dispatch_strategy


class ParallelExecutor:
    """Executes parallel fan-out, branch dispatch, and fan-in barrier.

    This is the core engine for splitting work into concurrent branches,
    running them (with either fail-fast or best-effort semantics), and
    collecting their outputs into a single merged result.
    """

    def split_fan_out(
        self,
        run_id: str,
        output_data: dict[str, Any],
        config: ParallelConfig,
        node: Any,
    ) -> list[BranchContext]:
        """Split a node's output into N branch contexts for parallel execution.

        Extracts the list at config.split_path from output_data, validates
        its shape, and creates one BranchContext per item.

        Args:
            run_id: The ID of the parent run.
            output_data: The node's output dictionary to split.
            config: Parallel configuration specifying split_path and constraints.
            node: The graph node being split (used for type validation).

        Returns:
            A list of BranchContext objects, one per split item.

        Raises:
            FanOutValidationError: If the node type is unsupported, the split_path
                is missing, the value is not a list, the list is empty, or the
                branch count exceeds max_branches.
        """
        # Validate node type -- HumanApprovalNode cannot be fanned out
        if hasattr(node, "node_type") and node.node_type == "human_approval":
            msg = "parallel fan-out is not supported on HumanApprovalNode"
            raise FanOutValidationError(msg)

        # Validate node type -- SubgraphNode cannot be fanned out
        if hasattr(node, "node_type") and node.node_type == "subgraph":
            msg = (
                "parallel fan-out is not supported on SubgraphNode"
                " -- subgraph composition inside parallel branches is not yet supported"
            )
            raise FanOutValidationError(msg)

        # Extract the list at split_path
        found, value = _get_path(output_data, config.split_path)
        if not found:
            msg = f"split_path '{config.split_path}' not found in output"
            raise FanOutValidationError(msg)

        if not isinstance(value, list):
            msg = f"value at split_path '{config.split_path}' is not a list"
            raise FanOutValidationError(msg)

        if len(value) == 0:
            msg = "split_path resolved to an empty list"
            raise FanOutValidationError(msg)

        # Enforce max_branches cap
        if config.max_branches is not None and len(value) > config.max_branches:
            msg = f"branch count {len(value)} exceeds max_branches {config.max_branches}"
            raise FanOutValidationError(msg)

        # Create one BranchContext per item
        return [
            BranchContext(
                branch_index=i,
                branch_id=f"{run_id}:branch:{i}",
                input_payload=item if isinstance(item, dict) else {"_item": item},
            )
            for i, item in enumerate(value)
        ]

    async def execute_branches(
        self,
        branch_contexts: list[BranchContext],
        branch_coro_factory: Callable[[BranchContext], Coroutine[Any, Any, dict[str, Any]]],
        config: ParallelConfig,
    ) -> list[BranchResult]:
        """Run all branches concurrently and collect results.

        In best-effort mode, all branches run to completion regardless of
        individual failures. In fail-fast mode, the first exception cancels
        all remaining branches.

        Args:
            branch_contexts: The contexts for each branch to execute.
            branch_coro_factory: A callable that creates a coroutine for each branch.
            config: Parallel configuration controlling fail behavior.

        Returns:
            A list of BranchResult objects, ordered by branch_index.

        Raises:
            ParallelExecutionError: In fail-fast mode, wraps the first branch exception.
        """
        if config.fail_mode == "best_effort":
            return await self._execute_best_effort(branch_contexts, branch_coro_factory)
        return await self._execute_fail_fast(branch_contexts, branch_coro_factory)

    async def _execute_best_effort(
        self,
        branch_contexts: list[BranchContext],
        branch_coro_factory: Callable[[BranchContext], Coroutine[Any, Any, dict[str, Any]]],
    ) -> list[BranchResult]:
        """Run all branches, collecting both successes and failures."""
        raw_results = await asyncio.gather(
            *[branch_coro_factory(ctx) for ctx in branch_contexts],
            return_exceptions=True,
        )

        branch_results: list[BranchResult] = []
        for ctx, result in zip(branch_contexts, raw_results, strict=True):
            if isinstance(result, BaseException):
                branch_results.append(
                    BranchResult(
                        branch_index=ctx.branch_index,
                        output=None,
                        error=str(result),
                    )
                )
            else:
                branch_results.append(
                    BranchResult(
                        branch_index=ctx.branch_index,
                        output=result,
                        error=None,
                    )
                )
        return branch_results

    async def _execute_fail_fast(
        self,
        branch_contexts: list[BranchContext],
        branch_coro_factory: Callable[[BranchContext], Coroutine[Any, Any, dict[str, Any]]],
    ) -> list[BranchResult]:
        """Run all branches, cancelling remaining on first failure."""
        tasks = [asyncio.create_task(branch_coro_factory(ctx)) for ctx in branch_contexts]

        try:
            raw_results = await asyncio.gather(*tasks)
        except Exception as exc:
            # Cancel all remaining tasks
            for task in tasks:
                if not task.done():
                    task.cancel()
            # Wait for cancellations to complete
            await asyncio.gather(*tasks, return_exceptions=True)
            msg = f"parallel execution failed (fail-fast): {exc}"
            raise ParallelExecutionError(msg) from exc

        return [
            BranchResult(
                branch_index=ctx.branch_index,
                output=result,
                error=None,
            )
            for ctx, result in zip(branch_contexts, raw_results, strict=True)
        ]

    def collect_fan_in(
        self,
        branch_results: list[BranchResult],
        config: ParallelConfig,
        base_output: dict[str, Any],
    ) -> FanInResult:
        """Aggregate branch results into a single FanInResult.

        Sorts results by branch_index for deterministic ordering, builds
        the output list (with None for failed branches per D-08), and merges
        it into the base output at the merge path.

        Args:
            branch_results: Results from all branches.
            config: Parallel configuration specifying merge behavior.
            base_output: The base output dict to merge results into.

        Returns:
            A FanInResult with merged output, cost, and step totals.
        """
        # Sort by branch_index for deterministic ordering
        sorted_results = sorted(branch_results, key=lambda r: r.branch_index)

        # Build the output list (None preserved for failed branches per D-19)
        output_list = [r.output for r in sorted_results]

        # Dispatch through the strategy registry (D-01, D-02, D-04 literal).
        # For ``collect`` this produces an equivalent list-of-outputs (backward
        # compatible with Phase 38 semantics). For ``merge``/``reduce``/``custom``
        # it produces the reduced value to write at the merge path.
        reduced_value = dispatch_strategy(
            config.merge_strategy,
            output_list,
            reducer_ref=config.reducer_ref,
        )

        # Merge into base output at the merge path (defaults to split_path)
        merge_path = config.split_path
        merged_output = dict(base_output)
        _set_path(merged_output, merge_path, reduced_value)

        # Aggregate cost and step counts
        total_cost = sum(r.cost_usd for r in sorted_results)
        total_steps = sum(len(r.execution_history) for r in sorted_results)

        return FanInResult(
            results=sorted_results,
            merged_output=merged_output,
            total_cost_usd=total_cost,
            total_steps=total_steps,
        )
