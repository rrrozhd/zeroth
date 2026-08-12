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

from zeroth.contracts.mappings.executor import _get_path, _set_path
from zeroth.platform.measurement import MeasurementState
from zeroth.runtime.parallel.errors import (
    BranchApprovalPauseSignal,
    FanOutValidationError,
    MultipleBranchPauseError,
    ParallelExecutionError,
)
from zeroth.runtime.parallel.models import (
    BranchContext,
    BranchResult,
    FanInResult,
    ParallelConfig,
)
from zeroth.runtime.parallel.reducers import dispatch_strategy


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

        Concurrency controls (all optional, all preserve per-branch audit because
        each branch still runs through the same coroutine factory):
        * ``config.max_concurrency`` — a semaphore bounding simultaneously-running
          branches (sliding window, no barrier).
        * ``config.branch_timeout_seconds`` — per-branch ``wait_for``; a timeout
          is just another branch failure.
        * ``config.batch_size`` — sequential waves; wave N+1 starts only after
          wave N completes. An approval pause inside a multi-wave fan-out is
          rejected loudly (earlier waves already ran their side effects).

        Returns:
            A list of BranchResult objects, ordered by branch_index.

        Raises:
            ParallelExecutionError: In fail-fast mode, wraps the first branch
                exception; also raised when an approval pause occurs inside a
                multi-wave (batched) fan-out.
        """
        factory = self._with_concurrency_controls(branch_coro_factory, config)
        waves = self._split_into_waves(branch_contexts, config.batch_size)
        if len(waves) == 1:
            # Single wave — no barrier. Pause/partition semantics are unchanged
            # from the pre-batching engine, so approval pauses propagate normally.
            return await self._execute_window(waves[0], factory, config)

        # Multi-wave (batched): run each wave to completion before the next.
        all_results: list[BranchResult] = []
        for wave_index, wave in enumerate(waves):
            try:
                all_results.extend(await self._execute_window(wave, factory, config))
            except BranchApprovalPauseSignal as pause:
                # Earlier waves already executed their side effects; the resume
                # contract (a single paused branch) cannot represent completed
                # waves, so fail loudly rather than silently corrupt resume state.
                raise ParallelExecutionError(
                    f"approval pause inside a batched fan-out (batch_size="
                    f"{config.batch_size}): wave {wave_index} paused for approval "
                    f"after {wave_index} earlier wave(s) already ran their side "
                    "effects, and batched resume cannot represent partially-"
                    "completed waves. Move the approval gate outside the fan-out, "
                    "or drop batch_size."
                ) from pause
        return all_results

    @staticmethod
    def _split_into_waves(
        branch_contexts: list[BranchContext], batch_size: int | None
    ) -> list[list[BranchContext]]:
        """Chunk branches into sequential waves of at most ``batch_size``.

        ``None`` or a size covering everything yields a single wave — identical
        to the historical single-``gather`` behaviour.
        """
        if batch_size is None or batch_size >= len(branch_contexts):
            return [branch_contexts]
        return [
            branch_contexts[i : i + batch_size] for i in range(0, len(branch_contexts), batch_size)
        ]

    @staticmethod
    def _with_concurrency_controls(
        factory: Callable[[BranchContext], Coroutine[Any, Any, dict[str, Any]]],
        config: ParallelConfig,
    ) -> Callable[[BranchContext], Coroutine[Any, Any, dict[str, Any]]]:
        """Wrap a branch factory with an optional semaphore + per-branch timeout.

        Transparent to the pause/partition logic below: the wrapped coroutine
        raises exactly what the inner one raises (``BranchApprovalPauseSignal``
        passes straight through ``wait_for``); only a genuine timeout surfaces as
        ``TimeoutError``, which the fail-mode handlers already treat as a branch
        failure. Returns the factory unchanged when neither control is set.
        """
        semaphore = (
            asyncio.Semaphore(config.max_concurrency)
            if config.max_concurrency is not None
            else None
        )
        timeout = config.branch_timeout_seconds
        if semaphore is None and timeout is None:
            return factory

        async def wrapped(ctx: BranchContext) -> dict[str, Any]:
            async def _run() -> dict[str, Any]:
                if timeout is not None:
                    return await asyncio.wait_for(factory(ctx), timeout)
                return await factory(ctx)

            if semaphore is not None:
                async with semaphore:
                    return await _run()
            return await _run()

        return wrapped

    async def _execute_window(
        self,
        branch_contexts: list[BranchContext],
        branch_coro_factory: Callable[[BranchContext], Coroutine[Any, Any, dict[str, Any]]],
        config: ParallelConfig,
    ) -> list[BranchResult]:
        """Run one wave of branches under the configured fail mode."""
        if config.fail_mode == "best_effort":
            return await self._execute_best_effort(branch_contexts, branch_coro_factory)
        return await self._execute_fail_fast(branch_contexts, branch_coro_factory)

    async def _execute_best_effort(
        self,
        branch_contexts: list[BranchContext],
        branch_coro_factory: Callable[[BranchContext], Coroutine[Any, Any, dict[str, Any]]],
    ) -> list[BranchResult]:
        """Run all branches, collecting both successes and failures.

        D-11: If any branch raised ``BranchApprovalPauseSignal``, all
        in-flight siblings are considered cancelled and the signal is
        re-raised with ``completed_branch_results`` /
        ``cancelled_branch_contexts`` attached so
        ``RuntimeParallelExecutor.execute_fan_out`` can stash run-wide
        pause state.
        """
        tasks = [asyncio.create_task(branch_coro_factory(ctx)) for ctx in branch_contexts]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        # D-11: detect pause signal and hand control to the caller.
        # best_effort runs every branch to completion, so MORE THAN ONE branch can
        # pause (audit B10). Collect them all instead of overwriting a scalar —
        # keeping only the last would orphan the earlier paused branches' child
        # runs and drop them from the fan-in.
        pause_signals: list[BranchApprovalPauseSignal] = []
        completed_results: list[BranchResult] = []
        for ctx, result in zip(branch_contexts, raw_results, strict=True):
            if isinstance(result, BranchApprovalPauseSignal):
                pause_signals.append(result)
            elif isinstance(result, BaseException):
                # In best-effort semantics a regular Exception becomes a
                # BranchResult with error; in-flight pauses mean this
                # sibling was cancelled by the pause handler.
                completed_results.append(
                    BranchResult(
                        branch_index=ctx.branch_index,
                        output=None,
                        error=str(result),
                    )
                )
            else:
                completed_results.append(
                    BranchResult(
                        branch_index=ctx.branch_index,
                        output=result,
                        error=None,
                    )
                )

        if len(pause_signals) > 1:
            # More than one branch reached an approval gate. The resume contract
            # (pending_parallel_subgraph) carries a SINGLE paused branch, so
            # persisting just one would orphan the others (audit B10). Fail loudly
            # rather than silently corrupt state — list-based multi-pause resume is
            # the real fix and is out of scope for this hardening pass.
            raise MultipleBranchPauseError(
                f"{len(pause_signals)} branches paused for approval in a best_effort "
                "fan-out; concurrent multi-branch approval pauses are not yet "
                "supported (would orphan child runs). Use fail_fast, or move the "
                "approval gate outside the fan-out."
            )

        pause_signal = pause_signals[0] if pause_signals else None
        if pause_signal is not None:
            # Any sibling that didn't complete before pause went into
            # `completed_results` as an error BranchResult (via
            # CancelledError) — but those are cleanly distinguishable
            # from the paused branch. The outer caller rebuilds pause
            # state from the completed/cancelled partitioning. We put
            # everything non-paused into `completed_results`; the
            # runtime layer treats any entry NOT matching
            # pause.branch_index as a completed sibling. Explicit
            # cancellation partitioning is handled by the runtime.
            # Attach the raw partitioning as attributes so the runtime
            # can consume them deterministically.
            completed_before_pause: list[BranchResult] = [
                br for br in completed_results if br.error is None
            ]
            cancelled_by_pause: list[BranchContext] = [
                ctx
                for ctx, br in zip(
                    [c for c in branch_contexts if c.branch_index != pause_signal.branch_index],
                    [br for br in completed_results],
                    strict=False,
                )
                if br.error is not None
            ]
            # Stash these on the exception instance for the runtime.
            pause_signal.completed_branch_results = completed_before_pause  # type: ignore[attr-defined]
            pause_signal.cancelled_branch_contexts = cancelled_by_pause  # type: ignore[attr-defined]
            raise pause_signal

        return completed_results

    async def _execute_fail_fast(
        self,
        branch_contexts: list[BranchContext],
        branch_coro_factory: Callable[[BranchContext], Coroutine[Any, Any, dict[str, Any]]],
    ) -> list[BranchResult]:
        """Run all branches, cancelling remaining on first failure.

        D-11: ``BranchApprovalPauseSignal`` is a ``BaseException`` and
        propagates unwrapped so the runtime layer can stash run-wide
        pause state. Regular ``Exception`` failures are still wrapped
        in ``ParallelExecutionError`` to preserve Phase 38 semantics.
        """
        tasks = [asyncio.create_task(branch_coro_factory(ctx)) for ctx in branch_contexts]

        try:
            raw_results = await asyncio.gather(*tasks)
        except BranchApprovalPauseSignal as pause:
            # Cancel in-flight siblings, drain, and re-raise the pause.
            for task in tasks:
                if not task.done():
                    task.cancel()
            drained = await asyncio.gather(*tasks, return_exceptions=True)
            pause_signals = [
                result
                for result in drained
                if isinstance(result, BranchApprovalPauseSignal)
            ]
            if len(pause_signals) > 1:
                raise MultipleBranchPauseError(
                    f"{len(pause_signals)} branches paused for approval in a fail_fast "
                    "fan-out; concurrent multi-branch approval pauses are not yet supported"
                ) from pause
            # Partition: completed-before-pause vs cancelled in-flight.
            completed_before_pause: list[BranchResult] = []
            cancelled_by_pause: list[BranchContext] = []
            for ctx, result in zip(branch_contexts, drained, strict=True):
                if ctx.branch_index == pause.branch_index:
                    continue  # the paused branch itself
                if isinstance(result, BranchApprovalPauseSignal):
                    continue
                elif isinstance(result, BaseException):
                    # CancelledError or other -- treat as cancelled.
                    cancelled_by_pause.append(ctx)
                else:
                    completed_before_pause.append(
                        BranchResult(
                            branch_index=ctx.branch_index,
                            output=result,
                            error=None,
                        )
                    )
            pause.completed_branch_results = completed_before_pause  # type: ignore[attr-defined]
            pause.cancelled_branch_contexts = cancelled_by_pause  # type: ignore[attr-defined]
            raise
        except Exception as exc:
            # Cancel all remaining tasks
            for task in tasks:
                if not task.done():
                    task.cancel()
            # Wait for cancellations to complete
            await asyncio.gather(*tasks, return_exceptions=True)
            msg = f"parallel execution failed (fail-fast): {exc}"
            error = ParallelExecutionError(msg)
            error.branch_histories = [  # type: ignore[attr-defined]
                (list(ctx.execution_history), list(ctx.audit_refs)) for ctx in branch_contexts
            ]
            raise error from exc

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
        costs = [result.cost_usd for result in sorted_results if result.cost_usd is not None]
        estimates = [
            result.estimated_cost_usd
            for result in sorted_results
            if result.estimated_cost_usd is not None
        ]
        states = [result.cost_measurement for result in sorted_results]
        cost_measurement = (
            MeasurementState.UNMEASURED
            if not states or MeasurementState.UNMEASURED in states
            else MeasurementState.ESTIMATED
            if MeasurementState.ESTIMATED in states
            else MeasurementState.MEASURED
        )
        total_steps = sum(len(r.execution_history) for r in sorted_results)

        return FanInResult(
            results=sorted_results,
            merged_output=merged_output,
            total_cost_usd=sum(costs) if costs else None,
            total_estimated_cost_usd=sum(estimates) if estimates else None,
            cost_measurement=cost_measurement,
            total_steps=total_steps,
        )
