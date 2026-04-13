# Parallel Execution

*Added in v4.0*

Parallel fan-out/fan-in lets a single node spawn N parallel branches from its output, execute them concurrently via asyncio, and merge results at a synchronization barrier with deterministic ordering.

## How It Works

A node configured with a `ParallelConfig` produces a list of items. The `ParallelExecutor` splits this list into branches, creates isolated `BranchContext` instances for each branch, and runs them concurrently. A synchronization barrier collects `BranchResult` objects in branch-index order, producing a `FanInResult`. Two execution modes are supported: **best-effort** (all branches run to completion, failures collected) and **fail-fast** (first failure cancels remaining branches).

## Key Components

- **`ParallelExecutor`** -- Orchestrates the full fan-out/fan-in lifecycle: validates the configuration, splits work, runs branches concurrently, and merges results.
- **`ParallelConfig`** -- Configuration model specifying branch count, execution mode (best-effort or fail-fast), and step limits.
- **`BranchContext`** -- Per-branch execution context carrying isolated visit counts, audit trail, failure tracking, and cost attribution.
- **`BranchResult`** -- Output of a single branch execution, including the result payload, branch index, timing, and any errors.
- **`FanInResult`** -- Merged output from all branches, ordered by branch index, with aggregate metadata.
- **`GlobalStepTracker`** -- Shared counter across all branches to enforce a global step limit, preventing runaway parallel execution.

## Branch Isolation

Each branch gets its own:

- Visit counts (loop detection is per-branch)
- Audit trail entries
- Failure tracking
- Cost attribution (BudgetEnforcer pre-reserves per branch)

Policy, audit, and contract validation apply independently per branch.

## Known Limitations

**SubgraphNode cannot be used inside parallel branches.** If a parallel fan-out source node has a SubgraphNode as a downstream node, the fan-out is rejected with a `FanOutValidationError`. This restriction exists because subgraph execution requires the full orchestrator `_drive()` loop, which is not available inside branch coroutines. Subgraph composition and parallel execution work independently but cannot be nested.

## Error Handling

- **`FanOutValidationError`** -- Raised when parallel configuration is invalid (e.g., SubgraphNode in branches, invalid branch count).
- **`BranchError`** -- Raised when an individual branch fails during execution.
- **`ParallelExecutionError`** -- Raised for orchestration-level failures in the parallel executor.
- **`ParallelStepLimitError`** -- Raised when the global step limit is exceeded across all branches.

See the [API Reference](../reference/http-api.md) for endpoint details and the source code under `zeroth.core.parallel` for implementation.
