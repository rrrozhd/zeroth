"""Fan-out and fan-in for the orchestration runtime.

:class:`RuntimeParallelExecutor` owns the parallel path: splitting a node's
output into branch contexts, running each branch's downstream nodes
concurrently against branch-isolated state, and merging the results back into
the parent run. It also owns the D-11 approval pause — the case where an
approval gate inside a subgraph branch has to stop the *whole* run, and the
parent must persist enough state that resuming re-enters only the paused child.

The heavy lifting of scheduling and merge strategy stays with
``zeroth.runtime.parallel.executor.ParallelExecutor``; this collaborator owns what
the *runtime* does around it — governance per branch, branch audit records,
cost rollup, and pause/resume state.

``orchestrator`` is an explicit dependency rather than an ambient one:
``SubgraphExecutor.execute`` takes the orchestrator by keyword as part of its
own published contract, so a branch that runs a subgraph has to hand one over.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from zeroth.contracts.governed import RunStatus
from zeroth.contracts.graph import Graph, Node, SubgraphNode
from zeroth.governance.audit import NodeAuditRecord
from zeroth.platform.measurement import MeasurementState
from zeroth.runtime.orchestration.audit_recorder import RuntimeAuditRecorder
from zeroth.runtime.orchestration.dispatcher import NodeDispatcher
from zeroth.runtime.orchestration.errors import OrchestratorError
from zeroth.runtime.orchestration.policy_gate import RuntimePolicyGate
from zeroth.runtime.orchestration.tool_executor import node_by_id
from zeroth.runtime.parallel.errors import BranchApprovalPauseSignal, FanOutValidationError
from zeroth.runtime.parallel.executor import ParallelExecutor
from zeroth.runtime.parallel.models import (
    BranchContext,
    BranchResult,
    FanInResult,
    GlobalStepTracker,
)
from zeroth.runtime.runs import Run, RunHistoryEntry
from zeroth.runtime.runs.costs import rollup_cost_history, rollup_run_cost
from zeroth.runtime.subgraphs.resolver import merge_governance, namespace_subgraph


def sum_run_cost(run: Run) -> float | None:
    """Return the child Run's aggregated cost_usd for BranchResult rollup.

    Reads the `total_cost_usd` key written by `SubgraphExecutor.execute`
    on the child run's metadata at return-time (W-4 cost-rollup
    location). Falls back to walking `execution_history` entries for
    any `cost_usd` field if the explicit aggregation key is absent.
    The drive loop does NOT write this key — the only writer is
    `SubgraphExecutor.execute`.
    """
    if not run.execution_history and not {
        "total_cost_usd",
        "total_estimated_cost_usd",
        "cost_measurement",
    }.intersection(run.metadata):
        return 0.0
    return rollup_run_cost(run).total_usd


def _dump_branch_context(ctx: BranchContext) -> dict[str, Any]:
    """Serialize the partial branch state needed after an approval pause."""
    return {
        "branch_index": ctx.branch_index,
        "branch_id": ctx.branch_id,
        "input_payload": dict(ctx.input_payload),
        "execution_history": [
            entry.model_dump(mode="json") if hasattr(entry, "model_dump") else entry
            for entry in ctx.execution_history
        ],
        "audit_refs": list(ctx.audit_refs),
        "metadata": dict(ctx.metadata),
    }


def _restore_branch_context(data: Mapping[str, Any], run_id: str) -> BranchContext:
    """Rehydrate the accounting-bearing subset persisted by `_dump_branch_context`."""
    branch_index = int(data.get("branch_index", -1))
    history = [
        RunHistoryEntry.model_validate(entry) if isinstance(entry, Mapping) else entry
        for entry in data.get("execution_history", [])
    ]
    return BranchContext(
        branch_index=branch_index,
        branch_id=str(data.get("branch_id", f"{run_id}:branch:{branch_index}")),
        input_payload=dict(data.get("input_payload", {})),
        execution_history=history,
        audit_refs=list(data.get("audit_refs", [])),
        metadata=dict(data.get("metadata", {})),
    )


@dataclass(frozen=True, slots=True)
class RuntimeParallelExecutor:
    """Runs a node's fan-out, collects the fan-in, and owns the D-11 pause."""

    run_repository: Any
    refresh_artifact_ttls: Callable[[Run], Awaitable[None]]
    parallel_executor: ParallelExecutor = ParallelExecutor()
    audit_recorder: RuntimeAuditRecorder = RuntimeAuditRecorder()
    node_dispatcher: NodeDispatcher | None = None
    policy_gate: RuntimePolicyGate | None = None
    subgraph_executor: Any = None
    budget_enforcer: Any = None
    orchestrator: Any = None
    plan_next_nodes: Callable[[Graph, Run, str, Mapping[str, Any]], list[str]] | None = None
    resume_graph: Callable[[Graph, str], Awaitable[Run]] | None = None

    @staticmethod
    def _enrich_branch_results(
        run: Run,
        contexts: list[BranchContext],
        results: list[BranchResult],
    ) -> None:
        """Transfer each completed branch's isolated history exactly once."""
        by_index = {ctx.branch_index: ctx for ctx in contexts}
        for result in results:
            ctx = by_index[result.branch_index]
            result.audit_refs = list(ctx.audit_refs)
            result.execution_history = list(ctx.execution_history)
            branch_cost = rollup_run_cost(
                run.model_copy(update={"metadata": {}, "execution_history": ctx.execution_history})
            )
            result.cost_usd = branch_cost.cost_usd
            result.estimated_cost_usd = branch_cost.estimated_cost_usd
            result.cost_measurement = branch_cost.cost_measurement

    async def execute_fan_out(
        self,
        graph: Graph,
        run: Run,
        node: Node,
        node_id: str,
        input_payload: Mapping[str, Any],
        output_data: dict[str, Any],
        audit_record: dict[str, Any],
        parallel_config: Any,
        *,
        step_tracker: GlobalStepTracker | None = None,
    ) -> FanInResult:
        """Execute parallel fan-out for a node with parallel_config.

        Splits the node's output into N branches, executes downstream nodes
        for each branch concurrently, and collects results into a FanInResult.
        Budget is checked before spawning. A GlobalStepTracker enforces the
        aggregate step limit across all branches.
        """
        from zeroth.runtime.parallel.models import ParallelConfig as _ParallelConfig

        config = (
            parallel_config
            if isinstance(parallel_config, _ParallelConfig)
            else _ParallelConfig.model_validate(
                parallel_config
                if isinstance(parallel_config, dict)
                else parallel_config.model_dump()
            )
        )

        # Split output into branch contexts
        branch_contexts = self.parallel_executor.split_fan_out(
            run.run_id,
            output_data,
            config,
            node,
        )

        # Budget pre-reservation before spawning branches
        if self.budget_enforcer is not None:
            allowed, current_spend, budget_cap = await self.budget_enforcer.check_budget(
                run.tenant_id,
            )
            if not allowed:
                raise FanOutValidationError(
                    f"budget exceeded for tenant {run.tenant_id}: "
                    f"spend=${current_spend:.4f} >= cap=${budget_cap:.4f}"
                )

        # Global step tracker: reuse parent composition's tracker when
        # provided (D-08, D-12) so nested fan-out inside a subgraph
        # decrements the same shared budget. Only construct a fresh
        # tracker at the top-level fan-out invocation.
        if step_tracker is None:
            step_tracker = GlobalStepTracker(
                current_steps=len(run.execution_history),
                max_steps=graph.execution_settings.max_total_steps,
            )

        # Determine downstream nodes from the fan-out source node
        assert self.plan_next_nodes is not None
        downstream_node_ids = self.plan_next_nodes(graph, run, node_id, output_data)

        async def branch_coro_factory(ctx: BranchContext) -> dict[str, Any]:
            """Execute downstream nodes for a single branch."""
            branch_output: dict[str, Any] = dict(ctx.input_payload)

            for ds_node_id in downstream_node_ids:
                ds_node = node_by_id(graph, ds_node_id)

                # Per-branch policy enforcement
                policy_result = (
                    await self.policy_gate.enforce_policy_for_branch(
                        graph,
                        run,
                        ds_node,
                        branch_output,
                    )
                    if self.policy_gate is not None
                    else None
                )
                if policy_result is not None:
                    raise RuntimeError(
                        f"policy denied branch {ctx.branch_index} node {ds_node_id}: "
                        f"{policy_result}"
                    )

                # D-05/D-23: SubgraphNode dispatch inside a fan-out branch.
                # Invokes SubgraphExecutor.execute with branch_context +
                # shared step_tracker; on approval pause, raises
                # BranchApprovalPauseSignal (BaseException subclass) to
                # propagate run-wide pause semantics (D-11).
                if isinstance(ds_node, SubgraphNode):
                    if self.subgraph_executor is None:
                        raise RuntimeError(
                            f"branch {ctx.branch_index}: SubgraphExecutor not configured"
                        )
                    ctx.metadata["subgraph_input"] = self.audit_recorder.redact(
                        dict(branch_output)
                    )
                    try:
                        child_run = await self.subgraph_executor.execute(
                            orchestrator=self.orchestrator,
                            parent_graph=graph,
                            parent_run=run,
                            node=ds_node,
                            node_id=ds_node_id,
                            input_payload=dict(branch_output),
                            branch_context=ctx,
                            step_tracker=step_tracker,
                        )
                    except Exception as exc:
                        await self.audit_recorder.record_failed_branch_execution(
                            run, ds_node, ds_node_id, branch_output, exc, ctx
                        )
                        raise
                    if child_run.status == RunStatus.WAITING_APPROVAL:
                        # D-11: propagate via BaseException so fail-fast
                        # gather re-raises, and best-effort inspects results.
                        raise BranchApprovalPauseSignal(
                            branch_index=ctx.branch_index,
                            child_run_id=child_run.run_id,
                            graph_ref=ds_node.subgraph.graph_ref,
                            version=ds_node.subgraph.version,
                            node_id=ds_node_id,
                        )
                    child_cost = rollup_run_cost(child_run)
                    if child_run.status != RunStatus.COMPLETED:
                        # A failed child must fail the branch (and, under
                        # fail_fast, the fan-out) — never fan-in as {}.
                        failure = child_run.failure_state
                        detail = failure.message if failure is not None else "unknown failure"
                        error = RuntimeError(
                            f"branch {ctx.branch_index}: subgraph child run "
                            f"{child_run.run_id} ended {child_run.status.value}: {detail}"
                        )
                        error.audit_record = {  # type: ignore[attr-defined]
                            "subgraph_run_id": child_run.run_id,
                            "subgraph_graph_ref": ds_node.subgraph.graph_ref,
                            "subgraph_status": child_run.status.value,
                            "cost_usd": child_cost.cost_usd,
                            "estimated_cost_usd": child_cost.estimated_cost_usd,
                            "cost_measurement": child_cost.cost_measurement,
                        }
                        await self.audit_recorder.record_failed_branch_execution(
                            run, ds_node, ds_node_id, branch_output, error, ctx
                        )
                        raise error
                    child_output = child_run.final_output or {}
                    if not isinstance(child_output, dict):
                        child_output = {"result": child_output}
                    ds_output = child_output
                    ds_audit = {
                        "subgraph_run_id": child_run.run_id,
                        "subgraph_graph_ref": ds_node.subgraph.graph_ref,
                        "subgraph_status": child_run.status.value,
                        "cost_usd": child_cost.cost_usd,
                        "estimated_cost_usd": child_cost.estimated_cost_usd,
                        "cost_measurement": child_cost.cost_measurement,
                    }
                else:
                    # Dispatch the downstream node with branch-isolated payload
                    try:
                        assert self.node_dispatcher is not None
                        ds_output, ds_audit = await self.node_dispatcher.dispatch(
                            ds_node, run, branch_output, graph
                        )
                    except Exception as exc:
                        await self.audit_recorder.record_failed_branch_execution(
                            run, ds_node, ds_node_id, branch_output, exc, ctx
                        )
                        raise

                # Increment global step tracker
                await step_tracker.increment()

                # Add branch_id to audit metadata
                ds_audit_with_branch = dict(ds_audit)
                ds_audit_with_branch["branch_id"] = ctx.branch_id
                ds_audit_with_branch["branch_index"] = ctx.branch_index

                # Record to branch-isolated state
                audit_ref = self.audit_recorder.next_branch_audit_ref(run, ctx)
                ctx.audit_refs.append(audit_ref)

                # Redact the branch snapshots once so BOTH the audit record and
                # the run-history entry persist redacted — resolved secrets in a
                # fan-out branch must not reach the stored run record (execution
                # history) or the typed audit columns.
                redacted_branch_input = self.audit_recorder.redact(dict(branch_output))
                redacted_branch_output = self.audit_recorder.redact(dict(ds_output))

                redacted_branch_audit = self.audit_recorder.redact(dict(ds_audit_with_branch))
                # Write audit record if audit repo available
                if self.audit_recorder.audit_repository is not None:
                    branch_tool_calls, branch_memory = self.audit_recorder.typed_fields(
                        redacted_branch_audit
                    )
                    await self.audit_recorder.audit_repository.write(
                        NodeAuditRecord(
                            audit_id=audit_ref,
                            run_id=run.run_id,
                            thread_id=run.thread_id,
                            tenant_id=run.tenant_id,
                            workspace_id=run.workspace_id,
                            node_id=ds_node_id,
                            node_version=ds_node.node_version,
                            graph_version_ref=run.graph_version_ref,
                            deployment_ref=run.deployment_ref,
                            attempt=1,
                            status="completed",
                            completed_at=datetime.now(UTC),
                            input_snapshot=redacted_branch_input,
                            output_snapshot=redacted_branch_output,
                            execution_metadata=redacted_branch_audit,
                            cost_usd=redacted_branch_audit.get("cost_usd"),
                            estimated_cost_usd=redacted_branch_audit.get("estimated_cost_usd"),
                            cost_measurement=redacted_branch_audit.get("cost_measurement"),
                            tool_calls=branch_tool_calls,
                            memory_interactions=branch_memory,
                        )
                    )

                # Append to branch execution history (redacted, matching the audit record)
                ctx.execution_history.append(
                    RunHistoryEntry(
                        node_id=ds_node_id,
                        status="completed",
                        input_snapshot=redacted_branch_input,
                        output_snapshot=redacted_branch_output,
                        audit_ref=audit_ref,
                        cost_usd=redacted_branch_audit.get("cost_usd"),
                        estimated_cost_usd=redacted_branch_audit.get("estimated_cost_usd"),
                        cost_measurement=redacted_branch_audit.get("cost_measurement"),
                    )
                )

                # Track branch visit counts (isolated from parent)
                ctx.node_visit_counts[ds_node_id] = ctx.node_visit_counts.get(ds_node_id, 0) + 1

                branch_output = ds_output

            return branch_output

        # Execute all branches via the parallel executor
        try:
            branch_results = await self.parallel_executor.execute_branches(
                branch_contexts,
                branch_coro_factory,
                config,
            )
        except BranchApprovalPauseSignal as pause:
            # D-11 literal: build a pause_state FanInResult so the
            # runtime can stash pending_parallel_subgraph metadata and
            # return the parent run in WAITING_APPROVAL.
            paused_ctx = next(
                (c for c in branch_contexts if c.branch_index == pause.branch_index),
                None,
            )
            completed_results = list(
                getattr(pause, "completed_branch_results", []) or []
            )
            self._enrich_branch_results(run, branch_contexts, completed_results)
            pause_state: dict[str, Any] = {
                "paused": {
                    "branch_index": pause.branch_index,
                    "child_run_id": pause.child_run_id,
                    "graph_ref": pause.graph_ref,
                    "version": pause.version,
                    "node_id": pause.node_id,
                    "branch_context": (
                        _dump_branch_context(paused_ctx)
                        if paused_ctx is not None
                        else None
                    ),
                },
                "completed_branch_results": completed_results,
                "cancelled_branch_contexts": [
                    _dump_branch_context(cctx)
                    for cctx in getattr(pause, "cancelled_branch_contexts", []) or []
                ],
                "split_input": dict(output_data),
                "source_audit": dict(audit_record),
                "source_input": dict(input_payload),
            }
            return FanInResult(results=[], pause_state=pause_state)
        except Exception as exc:
            # Fail-fast never reaches fan-in, so preserve already-recorded branch
            # failures on the parent before the driver persists the failed run.
            failed_histories = getattr(
                exc,
                "branch_histories",
                [(list(ctx.execution_history), list(ctx.audit_refs)) for ctx in branch_contexts],
            )
            for history, refs in failed_histories:
                for entry in history:
                    if not entry.audit_ref or all(
                        existing.audit_ref != entry.audit_ref
                        for existing in run.execution_history
                    ):
                        run.execution_history.append(entry)
                for ref in refs:
                    if ref not in run.audit_refs:
                        run.audit_refs.append(ref)
            raise

        # Enrich results with branch state + per-branch cost rollup (D-09)
        self._enrich_branch_results(run, branch_contexts, branch_results)

        # Collect fan-in
        return self.parallel_executor.collect_fan_in(branch_results, config, output_data)

    async def handle_subgraph_pause(
        self,
        run: Run,
        node: Node,
        node_id: str,
        input_payload: Mapping[str, Any],
        output_data: dict[str, Any],
        fan_in_result: FanInResult,
    ) -> Run:
        """Stash pending_parallel_subgraph and return run in WAITING_APPROVAL.

        D-11 literal: when a branch inside a fan-out raised
        BranchApprovalPauseSignal, the parent run must persist enough
        state to resume byte-identically. Stashes:

        * ``node_id`` — the fan-out source node to resume.
        * ``split_input`` — snapshot of the fan-out input so downstream
          branches can be reconstructed if needed.
        * ``completed_branches`` — already-finished BranchResults that
          are rehydrated as-is on resume (NOT re-executed).
        * ``paused_branch`` — the branch + child_run_id that hit
          WAITING_APPROVAL; resumed via
          ``SubgraphExecutor.resume(paused_child_run_id, ...)``.
        * ``cancelled_branches`` — in-flight BranchContexts when the
          pause fired; recorded as None-output BranchResults on resume
          per D-19 (NOT re-executed).
        """
        assert fan_in_result.pause_state is not None
        pause_state = fan_in_result.pause_state
        completed_dumps = [
            {
                "branch_index": br.branch_index,
                "output": br.output,
                "error": br.error,
                "cost_usd": br.cost_usd,
                "estimated_cost_usd": br.estimated_cost_usd,
                "cost_measurement": br.cost_measurement,
                "audit_refs": list(br.audit_refs),
                "execution_history": [
                    e.model_dump(mode="json") if hasattr(e, "model_dump") else e
                    for e in br.execution_history
                ],
            }
            for br in pause_state.get("completed_branch_results", [])
        ]
        run.metadata["pending_parallel_subgraph"] = {
            "node_id": node_id,
            "split_input": pause_state.get("split_input", dict(output_data)),
            "completed_branches": completed_dumps,
            "paused_branch": pause_state["paused"],
            "cancelled_branches": pause_state.get("cancelled_branch_contexts", []),
            "source_audit": pause_state.get("source_audit", {}),
            "source_input": pause_state.get("source_input", dict(input_payload)),
        }
        run.status = RunStatus.WAITING_APPROVAL
        run.pending_node_ids.insert(0, node_id)
        run.touch()
        persisted = await self.run_repository.put(run)
        await self.run_repository.write_checkpoint(persisted)
        await self.refresh_artifact_ttls(persisted)
        return persisted

    async def execute_fan_out_resume(
        self,
        graph: Graph,
        run: Run,
        node: Node,
        node_id: str,
        pending: dict[str, Any],
        *,
        step_tracker: GlobalStepTracker | None,
    ) -> FanInResult:
        """D-11 literal resume: reuse completed, resume paused, None-out cancelled.

        * Completed siblings are rehydrated byte-identically from the
          stash (NOT re-executed).
        * The paused branch is resumed via
          ``SubgraphExecutor.resume(paused_child_run_id, ...)`` — this
          is the ONLY re-entry into any child Run. If that call is
          missing, fall back to ``orchestrator.resume_graph`` directly.
        * Cancelled siblings are recorded as
          ``BranchResult(output=None, error="cancelled_by_approval_pause")``
          per D-19.
        * The assembled branch-index-ordered results are passed through
          ``collect_fan_in`` with the node's merge strategy (collect
          preserves None entries).
        """
        from zeroth.runtime.parallel.models import ParallelConfig as _ParallelConfig

        parallel_config = getattr(node, "parallel_config", None)
        assert parallel_config is not None
        config = (
            parallel_config
            if isinstance(parallel_config, _ParallelConfig)
            else _ParallelConfig.model_validate(
                parallel_config
                if isinstance(parallel_config, dict)
                else parallel_config.model_dump()
            )
        )

        # 1. Rehydrate completed BranchResults from the stash.
        completed_results: list[BranchResult] = []
        for d in pending.get("completed_branches", []):
            history = d.get("execution_history", [])
            # History entries may be dicts — rebuild RunHistoryEntry where
            # possible, else keep as dict for downstream consumption.
            rebuilt_history: list[Any] = []
            for e in history:
                if isinstance(e, dict):
                    try:
                        rebuilt_history.append(RunHistoryEntry.model_validate(e))
                    except Exception:
                        rebuilt_history.append(e)
                else:
                    rebuilt_history.append(e)
            completed_results.append(
                BranchResult(
                    branch_index=d["branch_index"],
                    output=d.get("output"),
                    error=d.get("error"),
                    audit_refs=list(d.get("audit_refs", [])),
                    execution_history=rebuilt_history,
                    cost_usd=(float(d["cost_usd"]) if d.get("cost_usd") is not None else None),
                    estimated_cost_usd=(
                        float(d["estimated_cost_usd"])
                        if d.get("estimated_cost_usd") is not None
                        else None
                    ),
                    cost_measurement=d.get("cost_measurement", "unmeasured"),
                )
            )

        # 2. Resume paused branch via SubgraphExecutor.resume (or fallback).
        paused_info = pending["paused_branch"]
        paused_branch_index = paused_info["branch_index"]
        paused_child_run_id = paused_info["child_run_id"]
        paused_graph_ref = paused_info["graph_ref"]
        paused_version = paused_info.get("version")

        if self.subgraph_executor is None:
            raise OrchestratorError(
                "cannot resume pending_parallel_subgraph without SubgraphExecutor"
            )

        resume_fn = getattr(self.subgraph_executor, "resume", None)
        if resume_fn is not None:
            resumed_child_run = await resume_fn(
                orchestrator=self.orchestrator,
                parent_graph=graph,
                parent_run=run,
                paused_child_run_id=paused_child_run_id,
                branch_index=paused_branch_index,
                step_tracker=step_tracker,
            )
        else:
            # Fallback: re-resolve + re-namespace with SAME branch_index for
            # D-11 idempotency, then resume_graph directly on the child run.
            subgraph, _ = await self.subgraph_executor.resolver.resolve(
                paused_graph_ref,
                paused_version,
                tenant_id=run.tenant_id,
                workspace_id=run.workspace_id,
            )
            child_run = await self.run_repository.get(paused_child_run_id)
            depth = child_run.metadata.get("subgraph_depth", 1) if child_run else 1
            namespaced = namespace_subgraph(
                subgraph,
                paused_graph_ref,
                depth,
                branch_index=paused_branch_index,
            )
            merged = merge_governance(graph, namespaced)
            assert self.resume_graph is not None
            resumed_child_run = await self.resume_graph(merged, paused_child_run_id)

        if resumed_child_run.status == RunStatus.WAITING_APPROVAL:
            # Still waiting on a nested approval — keep parent paused.
            return FanInResult(
                results=[],
                pause_state={
                    "paused": paused_info,
                    "completed_branch_results": completed_results,
                    "cancelled_branch_contexts": pending.get("cancelled_branches", []),
                    "split_input": pending.get("split_input", {}),
                    "source_audit": pending.get("source_audit", {}),
                    "source_input": pending.get("source_input", {}),
                },
            )

        resumed_output = resumed_child_run.final_output or {}
        if not isinstance(resumed_output, dict):
            resumed_output = {"result": resumed_output}
        resumed_cost = rollup_run_cost(resumed_child_run)
        paused_ctx_data = paused_info.get("branch_context") or {
            "branch_index": paused_branch_index,
        }
        paused_ctx = _restore_branch_context(paused_ctx_data, run.run_id)
        audit_ref = self.audit_recorder.next_branch_audit_ref(run, paused_ctx)
        paused_node_id = paused_info.get("node_id", node_id)
        paused_node = node_by_id(graph, paused_node_id) if isinstance(graph, Graph) else node
        resumed_audit = {
            "subgraph_run_id": resumed_child_run.run_id,
            "subgraph_graph_ref": paused_graph_ref,
            "subgraph_status": resumed_child_run.status.value,
            "cost_usd": resumed_cost.cost_usd,
            "estimated_cost_usd": resumed_cost.estimated_cost_usd,
            "cost_measurement": resumed_cost.cost_measurement,
            "branch_id": paused_ctx.branch_id,
            "branch_index": paused_branch_index,
        }
        resume_input = paused_ctx.metadata.get("subgraph_input", paused_ctx.input_payload)
        if not isinstance(resume_input, Mapping):
            resume_input = paused_ctx.input_payload
        redacted_input = self.audit_recorder.redact(dict(resume_input))
        redacted_output = self.audit_recorder.redact(dict(resumed_output))
        if self.audit_recorder.audit_repository is not None:
            await self.audit_recorder.audit_repository.write(
                NodeAuditRecord(
                    audit_id=audit_ref,
                    run_id=run.run_id,
                    thread_id=run.thread_id,
                    tenant_id=run.tenant_id,
                    workspace_id=run.workspace_id,
                    node_id=paused_node_id,
                    node_version=paused_node.node_version,
                    graph_version_ref=run.graph_version_ref,
                    deployment_ref=run.deployment_ref,
                    attempt=1,
                    status="completed",
                    completed_at=datetime.now(UTC),
                    input_snapshot=redacted_input,
                    output_snapshot=redacted_output,
                    execution_metadata=resumed_audit,
                    cost_usd=resumed_cost.cost_usd,
                    estimated_cost_usd=resumed_cost.estimated_cost_usd,
                    cost_measurement=resumed_cost.cost_measurement,
                )
            )
        resumed_entry = RunHistoryEntry(
            node_id=paused_node_id,
            status="completed",
            input_snapshot=redacted_input,
            output_snapshot=redacted_output,
            audit_ref=audit_ref,
            cost_usd=resumed_cost.cost_usd,
            estimated_cost_usd=resumed_cost.estimated_cost_usd,
            cost_measurement=resumed_cost.cost_measurement,
        )
        paused_history = [*paused_ctx.execution_history, resumed_entry]
        paused_cost = rollup_cost_history(paused_history)
        paused_result = BranchResult(
            branch_index=paused_branch_index,
            output=resumed_output,
            error=None,
            audit_refs=[*paused_ctx.audit_refs, audit_ref],
            execution_history=paused_history,
            cost_usd=paused_cost.cost_usd,
            estimated_cost_usd=paused_cost.estimated_cost_usd,
            cost_measurement=paused_cost.cost_measurement,
        )

        # 3. Record cancelled siblings as None-output BranchResults (D-19).
        cancelled_results: list[BranchResult] = []
        for data in pending.get("cancelled_branches", []):
            cancelled_ctx = _restore_branch_context(data, run.run_id)
            cancelled_input = cancelled_ctx.metadata.get(
                "subgraph_input", cancelled_ctx.input_payload
            )
            if not isinstance(cancelled_input, Mapping):
                cancelled_input = cancelled_ctx.input_payload
            cancelled_history = [
                *cancelled_ctx.execution_history,
                RunHistoryEntry(
                    node_id=paused_node_id,
                    status="cancelled",
                    input_snapshot=self.audit_recorder.redact(dict(cancelled_input)),
                    output_snapshot={},
                    cost_measurement=MeasurementState.UNMEASURED,
                ),
            ]
            cancelled_cost = rollup_cost_history(cancelled_history)
            cancelled_results.append(
                BranchResult(
                    branch_index=cancelled_ctx.branch_index,
                    output=None,
                    error="cancelled_by_approval_pause",
                    audit_refs=list(cancelled_ctx.audit_refs),
                    execution_history=cancelled_history,
                    cost_usd=cancelled_cost.cost_usd,
                    estimated_cost_usd=cancelled_cost.estimated_cost_usd,
                    cost_measurement=cancelled_cost.cost_measurement,
                )
            )

        # 4. Merge into branch-index order and run through collect_fan_in.
        all_results = completed_results + [paused_result] + cancelled_results
        all_results.sort(key=lambda br: br.branch_index)
        return self.parallel_executor.collect_fan_in(
            all_results, config, pending.get("split_input", {})
        )

    def merge_fan_in_state(self, run: Run, fan_in_result: FanInResult) -> None:
        """Merge branch execution state back into the parent Run.

        Appends all branch execution_history entries and audit_refs to the
        parent run so that the full trace is visible in the run record.
        """
        for branch_result in fan_in_result.results:
            for entry in branch_result.execution_history:
                if not entry.audit_ref or all(
                    existing.audit_ref != entry.audit_ref for existing in run.execution_history
                ):
                    run.execution_history.append(entry)
            for ref in branch_result.audit_refs:
                if ref not in run.audit_refs:
                    run.audit_refs.append(ref)
        run.completed_steps = [entry.node_id for entry in run.execution_history]
