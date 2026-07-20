"""The graph driver for the orchestration runtime.

:class:`GraphDriver` owns the state-machine progression: the loop that pops the
next pending node, dispatches it, records history, plans and queues its
successors, and writes a checkpoint — plus the terminal transitions and every
pause point that returns a run mid-flight (human approval, side-effect
approval, a subgraph or fan-out branch waiting on one).

The order of those side effects is the contract, not an implementation detail:
a checkpoint written before its audit record changes what a crashed run
replays. ``tests/runtime/orchestration/test_characterization.py`` pins it.

Every collaborator arrives explicitly. ``orchestrator`` and ``resume_graph``
are the two that point back at the facade, and both are genuine external
contracts rather than convenience: ``SubgraphExecutor.execute`` takes the
orchestrator by keyword, and resuming a paused child run re-enters through the
facade's public entry point so the run span is opened the same way.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from zeroth.contracts.conditions import NextStepPlanner
from zeroth.contracts.conditions.models import ConditionContext, NextStepPlan, TraversalState
from zeroth.contracts.graph import Edge, Graph, HumanApprovalNode, SubgraphNode
from zeroth.contracts.mappings import MappingExecutor
from zeroth.core.runs import Run, RunFailureState, RunStatus
from zeroth.platform.observability import start_span
from zeroth.runtime.agents.errors import BudgetExceededError
from zeroth.runtime.orchestration.audit_recorder import RuntimeAuditRecorder
from zeroth.runtime.orchestration.dispatcher import NodeDispatcher
from zeroth.runtime.orchestration.errors import OrchestratorError
from zeroth.runtime.orchestration.parallel_executor import (
    RuntimeParallelExecutor,
    sum_run_cost,
)
from zeroth.runtime.orchestration.policy_gate import RuntimePolicyGate
from zeroth.runtime.orchestration.tool_executor import node_by_id
from zeroth.runtime.parallel.errors import FanOutValidationError, ParallelExecutionError
from zeroth.runtime.parallel.models import GlobalStepTracker
from zeroth.runtime.parallel.reducers import dispatch_strategy
from zeroth.runtime.subgraphs.errors import (
    SubgraphCycleError,
    SubgraphDepthLimitError,
    SubgraphExecutionError,
    SubgraphResolutionError,
)
from zeroth.runtime.subgraphs.resolver import merge_governance, namespace_subgraph

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GraphDriver:
    """Drives a run through its graph, one node at a time, to a terminal state."""

    run_repository: Any
    audit_recorder: RuntimeAuditRecorder = RuntimeAuditRecorder()
    node_dispatcher: NodeDispatcher | None = None
    policy_gate: RuntimePolicyGate | None = None
    parallel_runtime: RuntimeParallelExecutor | None = None
    branch_planner: NextStepPlanner = NextStepPlanner()
    mapping_executor: MappingExecutor = MappingExecutor()
    approval_service: Any = None
    subgraph_executor: Any = None
    webhook_service: Any = None
    artifact_store: Any = None
    per_run_cap_usd: float | None = None
    orchestrator: Any = None
    resume_graph: Callable[[Graph, str], Awaitable[Run]] | None = None

    async def refresh_artifact_ttls(self, run: Run) -> None:
        """Refresh TTLs on all artifact references found in run state.

        Scans execution history output_snapshots and final_output for
        ArtifactReference-shaped dicts, then refreshes each one's TTL
        on the configured artifact store. This is a no-op when
        artifact_store is None (backward compatibility).

        Never raises -- failures are logged but do not affect the run.
        """
        if self.artifact_store is None:
            return
        try:
            from zeroth.platform.artifacts.helpers import refresh_artifact_ttls

            combined: dict[str, Any] = {}
            for i, entry in enumerate(run.execution_history):
                combined[f"_history_{i}"] = entry.output_snapshot
            if run.final_output is not None:
                combined["_final_output"] = run.final_output
            await refresh_artifact_ttls(self.artifact_store, combined, ttl=3600)
        except Exception:
            logger.exception("artifact TTL refresh failed (non-fatal)")

    async def external_stop(self, run: Run) -> Run | None:
        """Detect an operator's out-of-band cancel/interrupt (audit F3).

        An operator can cancel a run (``FAILED``, via the admin API) or interrupt
        it (``WAITING_INTERRUPT``), which writes the persisted status directly. The
        drive loop holds an in-memory ``Run`` and blind-writes ``RUNNING`` on every
        node hop, so it must re-read the persisted status and stop — otherwise the
        next ``RUNNING`` write clobbers the operator's decision and the run drives
        to completion. Call this before every ``RUNNING`` write (and before marking
        the run ``COMPLETED``): returns the persisted run to stop on, or ``None`` to
        proceed. Neither status is ever produced by this loop (``WAITING_INTERRUPT``
        is admin-only; ``FAILED`` is terminal-and-return), so it can't false-positive.
        """
        fresh = await self.run_repository.get(run.run_id)
        if fresh is not None and fresh.status in (
            RunStatus.FAILED,
            RunStatus.WAITING_INTERRUPT,
        ):
            # Adopt the operator's terminal/paused status onto the in-memory run —
            # which already holds this hop's execution_history and the successors
            # queued for the next hop — and PERSIST it, rather than returning the
            # freshly-read row (whose pending_node_ids is the stale pre-dispatch
            # []). Otherwise a later FAILED->PENDING replay (or interrupt resume)
            # would start from an empty queue and be marked COMPLETED with the
            # remaining nodes silently skipped (F3 re-audit). save_run does not
            # touch lease columns, so cancel_run's cleared lease is preserved.
            run.status = fresh.status
            run.failure_state = fresh.failure_state
            run.touch()
            # Concurrency guard (F3 re-audit follow-up): an operator replay/resume
            # (FAILED->PENDING, WAITING_INTERRUPT->RUNNING) can land between the
            # read above and this write; the drive loop shares the event loop with
            # the API handlers (modular monolith). Re-read immediately before the
            # write and yield to the operator if they already moved the run out of
            # a stop state, rather than blind-writing the stale status back and
            # silently reverting their transition. This shrinks the race window to
            # these two adjacent DB round-trips (a residual micro-race remains, but
            # it self-heals: pending_node_ids is persisted correctly, so re-issuing
            # the replay resumes — the cost is a wasted replay cycle, not data
            # corruption).
            latest = await self.run_repository.get(run.run_id)
            if latest is not None and latest.status not in (
                RunStatus.FAILED,
                RunStatus.WAITING_INTERRUPT,
            ):
                return latest
            persisted = await self.run_repository.put(run)
            await self.run_repository.write_checkpoint(persisted)
            return persisted
        return None

    async def drive(
        self,
        graph: Graph,
        run: Run,
        *,
        step_tracker: GlobalStepTracker | None = None,
    ) -> Run:
        """Main loop that processes nodes one at a time until done.

        Keeps popping the next pending node, running it, planning the
        next steps, and repeating until there are no more nodes to run,
        or until a guard/policy/approval stops execution.
        """
        started_at = perf_counter()
        while True:
            # Cooperative cancellation (audit F3): observe an operator's out-of-band
            # cancel/interrupt before completing the run or dispatching the next node.
            stopped = await self.external_stop(run)
            if stopped is not None:
                return stopped

            failed_run = await self.policy_gate.enforce_loop_guards(graph, run, started_at)
            if failed_run is not None:
                return failed_run
            if not run.pending_node_ids:
                # B9 deadlock guard: a non-empty join_state here means a convergent
                # node is still waiting for an inbound edge that will never resolve
                # (e.g. an inbound edge from an unreachable source). Fail loud
                # rather than silently mark COMPLETED — which would drop the node
                # and everything downstream and fire a false run.completed webhook.
                # Legitimate joins clear their entry on dispatch/skip, so anything
                # left is genuinely stuck. (Flag-gated: join_state is only ever
                # populated when sequential_join_enabled is on.)
                stuck_joins = run.metadata.get("join_state") or {}
                if stuck_joins:
                    waiting = ", ".join(sorted(stuck_joins))
                    return await self.fail_run(
                        run,
                        "join_deadlock",
                        f"sequential join barrier could not complete: node(s) [{waiting}] "
                        "never received all inbound edges (unreachable source or "
                        "unresolvable convergence)",
                    )
                # No more work is queued, so the run can be closed out as successful.
                run.status = RunStatus.COMPLETED
                run.current_node_ids = []
                run.final_output = run.metadata.get("last_output")
                run.touch()
                persisted = await self.run_repository.put(run)
                await self.run_repository.write_checkpoint(persisted)
                await self.refresh_artifact_ttls(persisted)
                await self.emit_webhook(
                    "run.completed",
                    persisted,
                    {
                        "run_id": persisted.run_id,
                        "graph_version_ref": persisted.graph_version_ref,
                        "status": "completed",
                    },
                )
                return persisted

            node_id = run.pending_node_ids.pop(0)
            node = node_by_id(graph, node_id)
            # Each node consumes the payload that was prepared for it by the previous step.
            input_payload = self.payload_for(run, node_id)
            # Node dispatch time — threaded into the audit record so it reflects a
            # real wall-clock duration instead of completed_at == started_at.
            node_started_at = datetime.now(UTC)
            run.current_node_ids = [node_id]
            run.current_step = node_id
            run.touch()
            run = await self.run_repository.put(run)

            # D-11 literal: resume path for a parallel fan-out that was
            # paused due to an approval inside a subgraph branch.
            pending_psg = run.metadata.get("pending_parallel_subgraph")
            if pending_psg and pending_psg.get("node_id") == node_id:
                fan_in_resume = await self.parallel_runtime.execute_fan_out_resume(
                    graph,
                    run,
                    node,
                    node_id,
                    pending_psg,
                    step_tracker=step_tracker,
                )
                if fan_in_resume.pause_state is not None:
                    # Nested approval inside the resumed branch (audit B8). Persist
                    # the pause durably via the SAME handler as the first pause,
                    # not an in-memory re-queue. The fan-out node was already
                    # popped from pending_node_ids and persisted above, and
                    # worker.resume_graph does not capture the returned run — so a
                    # bare insert+return is lost: the reloaded row has empty
                    # pending_node_ids and the next drive marks the run COMPLETED
                    # (false run.completed webhook, dropped sibling/paused outputs).
                    return await self.parallel_runtime.handle_subgraph_pause(
                        run,
                        node,
                        node_id,
                        input_payload,
                        pending_psg.get("split_input", dict(input_payload)),
                        fan_in_resume,
                    )
                del run.metadata["pending_parallel_subgraph"]
                run.status = RunStatus.RUNNING
                # Merge branch state and continue post-fan-in flow.
                self.parallel_runtime.merge_fan_in_state(run, fan_in_resume)
                merged_output = fan_in_resume.merged_output
                downstream_ids = self.plan_next_nodes(graph, run, node_id, merged_output)
                for ds_id in downstream_ids:
                    self.increment_node_visit(run, ds_id)
                    post_fan_in_ids = self.plan_next_nodes(graph, run, ds_id, merged_output)
                    self.queue_next_nodes(graph, run, ds_id, merged_output, post_fan_in_ids)
                run.metadata["last_output"] = merged_output
                # Cooperative cancel across a resumed fan-in (audit F3 follow-up).
                stopped = await self.external_stop(run)
                if stopped is not None:
                    return stopped
                run.touch()
                run = await self.run_repository.put(run)
                await self.run_repository.write_checkpoint(run)
                continue

            pending_approval = await self.policy_gate.consume_side_effect_approval(
                run, node, input_payload
            )
            if pending_approval is not None:
                return pending_approval

            denial = await self.policy_gate.enforce_policy(graph, run, node, input_payload)
            if denial is not None:
                return denial

            side_effect_gate = await self.policy_gate.gate_policy_required_side_effects(
                run, node, input_payload
            )
            if side_effect_gate is not None:
                return side_effect_gate

            if isinstance(node, HumanApprovalNode):
                service = self.approval_service
                approval_id = None
                if service is not None:
                    # Store a separate approval record so a human can review it outside the run.
                    approval = await service.create_pending(
                        run=run,
                        node=node,
                        input_payload=dict(input_payload),
                    )
                    approval_id = approval.approval_id
                    await self.emit_webhook(
                        "approval.requested",
                        run,
                        {
                            "approval_id": approval.approval_id,
                            "run_id": run.run_id,
                            "node_id": node.node_id,
                            "sla_deadline": (
                                approval.sla_deadline.isoformat() if approval.sla_deadline else None
                            ),
                        },
                    )
                run.status = RunStatus.WAITING_APPROVAL
                # Put the same node back at the front so execution can resume from this gate.
                run.metadata["pending_approval"] = {
                    "node_id": node.node_id,
                    "input": input_payload,
                    "approval_id": approval_id,
                }
                run.pending_node_ids.insert(0, node.node_id)
                run.touch()
                persisted = await self.run_repository.put(run)
                await self.run_repository.write_checkpoint(persisted)
                await self.refresh_artifact_ttls(persisted)
                return persisted

            # Phase 39: Subgraph composition -- delegate to SubgraphExecutor.
            if isinstance(node, SubgraphNode):
                if self.subgraph_executor is None:
                    return await self.fail_run(
                        run,
                        "subgraph_not_configured",
                        "SubgraphExecutor not configured -- cannot execute SubgraphNode. "
                        "Wire SubgraphExecutor at bootstrap to enable subgraph composition.",
                    )

                # Path B: Resume after approval -- pending_subgraph metadata exists
                # for this node_id. Re-resolve the subgraph, then resume the child
                # run instead of creating a new one.
                pending_subgraph = run.metadata.get("pending_subgraph")
                if pending_subgraph and pending_subgraph.get("node_id") == node_id:
                    child_run_id = pending_subgraph["child_run_id"]
                    graph_ref = pending_subgraph["graph_ref"]
                    version = pending_subgraph.get("version")

                    # Re-resolve, re-namespace, re-merge governance (Graph objects
                    # are not persisted in metadata -- too large).
                    try:
                        subgraph, _ = await self.subgraph_executor.resolver.resolve(
                            graph_ref,
                            version,
                            tenant_id=run.tenant_id,
                            workspace_id=run.workspace_id,
                        )
                    except SubgraphResolutionError as exc:
                        return await self.fail_run(run, "subgraph_resume_failed", str(exc))

                    depth = run.metadata.get("subgraph_depth", 0) + 1
                    subgraph = namespace_subgraph(subgraph, graph_ref, depth)
                    subgraph = merge_governance(graph, subgraph)

                    # Resume the child run (not create a new one).
                    child_run = await self.resume_graph(subgraph, child_run_id)

                    if child_run.status == RunStatus.WAITING_APPROVAL:
                        # Still waiting (nested approval or another gate in subgraph).
                        # Stay paused -- pending_subgraph metadata already correct.
                        run.status = RunStatus.WAITING_APPROVAL
                        run.pending_node_ids.insert(0, node_id)
                        run.touch()
                        persisted = await self.run_repository.put(run)
                        await self.run_repository.write_checkpoint(persisted)
                        await self.refresh_artifact_ttls(persisted)
                        return persisted

                    if child_run.status != RunStatus.COMPLETED:
                        failure = child_run.failure_state
                        detail = failure.message if failure is not None else "unknown failure"
                        return await self.fail_run(
                            run,
                            "subgraph_execution_failed",
                            f"child run {child_run.run_id} ended "
                            f"{child_run.status.value}: {detail}",
                        )

                    # Child completed -- clear pending state, use output.
                    del run.metadata["pending_subgraph"]
                    run.status = RunStatus.RUNNING
                    output_data = child_run.final_output or {}
                    if not isinstance(output_data, dict):
                        output_data = {"result": output_data}

                    audit_record = {
                        "subgraph_run_id": child_run.run_id,
                        "subgraph_graph_ref": graph_ref,
                        "subgraph_status": child_run.status.value,
                        "subgraph_resumed": True,
                    }

                    # Continue normal post-node flow.
                    await self.audit_recorder.record_history(
                        run,
                        node,
                        node_id,
                        input_payload,
                        output_data,
                        audit_record,
                        started_at=node_started_at,
                    )
                    self.increment_node_visit(run, node_id)
                    self.advance_downstream(graph, run, node_id, output_data)
                    run.metadata["last_output"] = output_data
                    # Cooperative cancel across a resumed subgraph node (audit F3).
                    stopped = await self.external_stop(run)
                    if stopped is not None:
                        return stopped
                    run.touch()
                    persisted = await self.run_repository.put(run)
                    await self.run_repository.write_checkpoint(persisted)
                    await self.refresh_artifact_ttls(persisted)
                    continue

                # Path A: First encounter -- no pending_subgraph for this node.
                try:
                    with start_span(
                        "zeroth.subgraph",
                        {"zeroth.node_id": node_id, "zeroth.run_id": run.run_id},
                    ):
                        child_run = await self.subgraph_executor.execute(
                            orchestrator=self.orchestrator,
                            parent_graph=graph,
                            parent_run=run,
                            node=node,
                            node_id=node_id,
                            input_payload=input_payload,
                            step_tracker=step_tracker,
                        )
                except (
                    SubgraphDepthLimitError,
                    SubgraphResolutionError,
                    SubgraphExecutionError,
                    SubgraphCycleError,
                ) as exc:
                    return await self.fail_run(run, "subgraph_execution_failed", str(exc))

                # Check if child paused for approval -- propagate up.
                if child_run.status == RunStatus.WAITING_APPROVAL:
                    run.status = RunStatus.WAITING_APPROVAL
                    run.metadata["pending_subgraph"] = {
                        "child_run_id": child_run.run_id,
                        "node_id": node_id,
                        "graph_ref": node.subgraph.graph_ref,
                        "version": node.subgraph.version,
                    }
                    run.pending_node_ids.insert(0, node_id)  # Re-queue for resume
                    run.touch()
                    persisted = await self.run_repository.put(run)
                    await self.run_repository.write_checkpoint(persisted)
                    await self.refresh_artifact_ttls(persisted)
                    return persisted

                if child_run.status != RunStatus.COMPLETED:
                    failure = child_run.failure_state
                    detail = failure.message if failure is not None else "unknown failure"
                    return await self.fail_run(
                        run,
                        "subgraph_execution_failed",
                        f"child run {child_run.run_id} ended {child_run.status.value}: {detail}",
                    )

                # Use child run's final_output as this node's output.
                output_data = child_run.final_output or {}
                if not isinstance(output_data, dict):
                    output_data = {"result": output_data}

                audit_record = {
                    "subgraph_run_id": child_run.run_id,
                    "subgraph_graph_ref": node.subgraph.graph_ref,
                    "subgraph_status": child_run.status.value,
                    "subgraph_depth": child_run.metadata.get("subgraph_depth", 0),
                }

                # Record history and plan next nodes (same post-node flow as normal nodes).
                await self.audit_recorder.record_history(
                    run,
                    node,
                    node_id,
                    input_payload,
                    output_data,
                    audit_record,
                    started_at=node_started_at,
                )
                self.increment_node_visit(run, node_id)
                self.advance_downstream(graph, run, node_id, output_data)
                run.metadata["last_output"] = output_data
                # Cooperative cancel across a synchronous subgraph node (audit F3).
                stopped = await self.external_stop(run)
                if stopped is not None:
                    return stopped
                run.touch()
                persisted = await self.run_repository.put(run)
                await self.run_repository.write_checkpoint(persisted)
                await self.refresh_artifact_ttls(persisted)
                continue

            try:
                # Per-run cost ceiling (local, control-plane-independent). Post-hoc:
                # cumulative spend from prior nodes is read from the run's own audit
                # cost_usd, so the run halts on the NEXT node once it crosses the cap.
                if self.per_run_cap_usd is not None:
                    spent = sum_run_cost(run)
                    if spent >= self.per_run_cap_usd:
                        raise BudgetExceededError(
                            f"per-run budget exceeded: ${spent:.4f} >= ${self.per_run_cap_usd:.4f}",
                            spend=spent,
                            cap=self.per_run_cap_usd,
                        )
                output_data, audit_record = await self.node_dispatcher.dispatch(
                    node, run, input_payload, graph
                )
            except Exception as exc:
                await self.audit_recorder.record_failed_execution(
                    run, node, node_id, input_payload, exc, started_at=node_started_at
                )
                return await self.fail_run(run, "node_execution_failed", str(exc))

            # Phase 38: Parallel fan-out detection.
            parallel_config = getattr(node, "parallel_config", None)
            if parallel_config is not None:
                try:
                    with start_span(
                        "zeroth.fanout",
                        {"zeroth.node_id": node_id, "zeroth.run_id": run.run_id},
                    ):
                        fan_in_result = await self.parallel_runtime.execute_fan_out(
                            graph,
                            run,
                            node,
                            node_id,
                            input_payload,
                            output_data,
                            audit_record,
                            parallel_config,
                            step_tracker=step_tracker,
                        )
                except (FanOutValidationError, ParallelExecutionError) as exc:
                    return await self.fail_run(run, "parallel_execution_failed", str(exc))
                # D-11: Check for run-wide approval pause from a branch's subgraph.
                if fan_in_result.pause_state is not None:
                    return await self.parallel_runtime.handle_subgraph_pause(
                        run,
                        node,
                        node_id,
                        input_payload,
                        output_data,
                        fan_in_result,
                    )
                # Record the source node's own history (the node that triggered fan-out)
                await self.audit_recorder.record_history(
                    run,
                    node,
                    node_id,
                    input_payload,
                    output_data,
                    audit_record,
                    started_at=node_started_at,
                )
                self.increment_node_visit(run, node_id)
                # Merge branch histories and audit refs into parent run
                self.parallel_runtime.merge_fan_in_state(run, fan_in_result)
                # Use merged output for downstream planning.
                # The downstream nodes (one hop from source) were already executed
                # inside branches. Plan next from those downstream nodes instead.
                merged_output = fan_in_result.merged_output
                downstream_ids = self.plan_next_nodes(graph, run, node_id, output_data)
                for ds_id in downstream_ids:
                    self.increment_node_visit(run, ds_id)
                    post_fan_in_ids = self.plan_next_nodes(graph, run, ds_id, merged_output)
                    self.queue_next_nodes(graph, run, ds_id, merged_output, post_fan_in_ids)
                run.metadata["last_output"] = merged_output
                # Cooperative cancel across a parallel fan-in (audit F3 follow-up).
                stopped = await self.external_stop(run)
                if stopped is not None:
                    return stopped
                run.status = RunStatus.RUNNING
                run.current_node_ids = []
                run.touch()
                run = await self.run_repository.put(run)
                await self.run_repository.write_checkpoint(run)
                await self.refresh_artifact_ttls(run)
                continue

            await self.audit_recorder.record_history(
                run,
                node,
                node_id,
                input_payload,
                output_data,
                audit_record,
                started_at=node_started_at,
            )
            self.increment_node_visit(run, node_id)
            self.advance_downstream(graph, run, node_id, output_data)
            run.metadata["last_output"] = output_data
            # An operator may have cancelled/interrupted while this node was
            # dispatching; don't clobber that with our RUNNING write (audit F3).
            stopped = await self.external_stop(run)
            if stopped is not None:
                return stopped
            run.status = RunStatus.RUNNING
            run.current_node_ids = []
            run.touch()
            run = await self.run_repository.put(run)
            await self.run_repository.write_checkpoint(run)
            await self.refresh_artifact_ttls(run)

    def run_branch_planner(
        self,
        graph: Graph,
        run: Run,
        node_id: str,
        output_data: Mapping[str, Any],
    ) -> NextStepPlan:
        """Evaluate the source node's outgoing edges and record traversal bookkeeping.

        Returns the full :class:`NextStepPlan` (active + suppressed edge ids) so
        the caller can either take the legacy direct-queue path or the B9 join
        barrier. Mutates ``run`` exactly as the old ``plan_next_nodes`` did:
        extends ``condition_results``, bumps ``edge_visit_counts`` for active
        edges, updates ``path`` and ``terminal_reason``. This is the single
        source of truth for those side effects — ``plan_next_nodes`` and the
        join path both go through it, so behaviour cannot diverge.
        """
        traversal_state = TraversalState(
            node_visit_counts=dict(run.node_visit_counts),
            edge_visit_counts=dict(run.metadata.get("edge_visit_counts", {})),
            path=list(run.metadata.get("path", [])) + [node_id],
        )
        plan = self.branch_planner.plan(
            graph,
            node_id,
            ConditionContext(
                payload=dict(output_data),
                metadata={"run_id": run.run_id},
                node_visit_counts=dict(traversal_state.node_visit_counts),
                edge_visit_counts=dict(traversal_state.edge_visit_counts),
                path=list(traversal_state.path),
            ),
            traversal_state=traversal_state,
        )
        run.condition_results.extend(plan.branch_resolution.condition_results)
        edge_counts = dict(run.metadata.get("edge_visit_counts", {}))
        # Track edge usage so loops and branch history can be inspected later.
        for edge_id in plan.branch_resolution.active_edge_ids:
            edge_counts[edge_id] = edge_counts.get(edge_id, 0) + 1
        run.metadata["edge_visit_counts"] = edge_counts
        run.metadata["path"] = list(traversal_state.path)
        if plan.terminal_reason is not None:
            run.metadata["terminal_reason"] = plan.terminal_reason
        return plan

    def plan_next_nodes(
        self,
        graph: Graph,
        run: Run,
        node_id: str,
        output_data: Mapping[str, Any],
    ) -> list[str]:
        """Decide which nodes to run next based on the current node's output.

        Thin wrapper over :meth:`run_branch_planner` that returns only the
        active target ids — the historical contract used by the parallel
        fan-out/fan-in paths, which never take the join barrier.
        """
        return list(self.run_branch_planner(graph, run, node_id, output_data).next_node_ids)

    def queue_next_nodes(
        self,
        graph: Graph,
        run: Run,
        source_node_id: str,
        output_data: Mapping[str, Any],
        next_node_ids: list[str],
    ) -> None:
        """Add the next nodes to the pending queue with their input payloads.

        For each next node, applies any data mapping defined on the edge
        (transforming the output of the current node into the input for the
        next one) and adds it to the queue.
        """
        payloads = dict(run.metadata.get("node_payloads", {}))
        for target_node_id in next_node_ids:
            payloads[target_node_id] = self.edge_payload(
                graph, run, source_node_id, target_node_id, output_data
            )
            run.pending_node_ids.append(target_node_id)
        run.metadata["node_payloads"] = payloads

    def edge_payload(
        self,
        graph: Graph,
        run: Run,
        source_node_id: str,
        target_node_id: str,
        output_data: Mapping[str, Any],
        edge: Edge | None = None,
    ) -> dict[str, Any]:
        """Compute the input payload delivered across one edge (applying its mapping).

        Extracted from ``queue_next_nodes`` so the legacy direct-queue path and
        the B9 join barrier compute delivered payloads identically. ``edge`` may
        be passed to avoid a re-lookup when the caller already holds it.
        """
        if edge is None:
            edge = self.edge_for(graph, source_node_id, target_node_id)
        payload: dict[str, Any] = dict(output_data)
        if edge is not None and edge.mapping is not None:
            # Edge mappings reshape one node's output into the next node's expected input.
            context_ns = {
                "payload": dict(output_data),
                "state": dict(run.metadata.get("state", {})),
                "variables": dict(run.metadata.get("variables", {})),
                "node_visit_counts": dict(run.node_visit_counts),
                "edge_visit_counts": dict(run.metadata.get("edge_visit_counts", {})),
                "path": list(run.metadata.get("path", [])),
                "metadata": {"run_id": run.run_id},
            }
            payload = self.mapping_executor.execute(output_data, edge.mapping, context=context_ns)
        return payload

    # ------------------------------------------------------------------
    # B9 sequential join barrier
    # ------------------------------------------------------------------

    def advance_downstream(
        self,
        graph: Graph,
        run: Run,
        source_node_id: str,
        output_data: Mapping[str, Any],
    ) -> None:
        """Plan and enqueue the next nodes after ``source_node_id`` completes.

        Single entry point for the sequential (non-parallel) post-node flow.
        With ``sequential_join_enabled`` off (default) this is byte-identical to
        the historical ``plan_next_nodes`` + ``queue_next_nodes`` pairing. With
        the flag on it routes through the token/skip-propagation join barrier so a
        convergent node dispatches once per iteration after all its inbound edges
        resolve.
        """
        plan = self.run_branch_planner(graph, run, source_node_id, output_data)
        if not graph.execution_settings.sequential_join_enabled:
            self.queue_next_nodes(graph, run, source_node_id, output_data, list(plan.next_node_ids))
            return
        self._resolve_join_edges(graph, run, source_node_id, output_data, plan)

    def _resolve_join_edges(
        self,
        graph: Graph,
        run: Run,
        source_node_id: str,
        output_data: Mapping[str, Any],
        plan: NextStepPlan,
    ) -> None:
        """Seed the resolution worklist from one source node's outgoing edges (B9).

        Every non-tool outgoing edge is resolved as either DELIVERED (active,
        carries the mapped payload) or SUPPRESSED (condition false / disabled /
        visit-limited). Draining the worklist dispatches any now-fully-resolved
        convergent target and propagates the skip cascade.
        """
        resolution = plan.branch_resolution
        edge_map = {edge.edge_id: edge for edge in graph.edges}
        worklist: deque[tuple[Edge, bool, dict[str, Any] | None]] = deque()
        for edge_id in resolution.active_edge_ids:
            edge = edge_map.get(edge_id)
            if edge is None or edge.kind == "tool":
                continue
            payload = self.edge_payload(
                graph, run, source_node_id, edge.target_node_id, output_data, edge
            )
            worklist.append((edge, True, payload))
        for edge_id in resolution.suppressed_edge_ids:
            edge = edge_map.get(edge_id)
            if edge is None or edge.kind == "tool":
                continue
            worklist.append((edge, False, None))
        self._drain_join_worklist(graph, run, worklist)

    def _drain_join_worklist(
        self,
        graph: Graph,
        run: Run,
        worklist: deque[tuple[Edge, bool, dict[str, Any] | None]],
    ) -> None:
        """Resolve edges until the worklist drains, enqueuing ready join targets.

        A ``parallel_config`` target owns its own fan-in, so it bypasses the
        barrier (queued directly on delivery, exactly like the legacy path). Any
        edge resolved twice in a single drain signals a cyclic convergent node —
        those are rejected at publish validation under the flag, so this is
        defense-in-depth for unvalidated (code-authored) graphs: fail loud rather
        than spin.
        """
        visited_edges: set[str] = set()
        while worklist:
            edge, delivered, payload = worklist.popleft()
            if edge.edge_id in visited_edges:
                raise OrchestratorError(
                    f"join edge {edge.edge_id!r} resolved twice in one step — a cyclic "
                    "convergent node is unsupported under sequential_join_enabled"
                )
            visited_edges.add(edge.edge_id)
            target = edge.target_node_id
            node = node_by_id(graph, target)
            if getattr(node, "parallel_config", None) is not None:
                # Parallel fan-in nodes handle their own collect/reduce; the
                # sequential join barrier defers to them entirely.
                if delivered and payload is not None:
                    self._stash_join_payload(run, target, payload)
                    run.pending_node_ids.append(target)
                continue
            self._record_edge_resolution(run, target, edge.edge_id, delivered, payload)
            self._check_join_target(graph, run, target, worklist)

    def _check_join_target(
        self,
        graph: Graph,
        run: Run,
        target: str,
        worklist: deque[tuple[Edge, bool, dict[str, Any] | None]],
    ) -> None:
        """Dispatch or skip a target once all its inbound edges for this iteration resolve.

        - If every inbound edge is resolved and >=1 delivered: merge the
          delivered payloads via the node's ``JoinConfig`` and enqueue the node
          once. A single delivered edge (the common single-inbound case, and
          conditional reconvergence) skips the merge entirely — byte-identical to
          the legacy single-payload enqueue.
        - If every inbound edge is resolved and none delivered: the node is
          SKIPPED; its outbound edges cascade as SUPPRESSED so a downstream join
          learns the branch is resolved-not-pending.
        - Otherwise the node keeps waiting.
        """
        inbound = self._inbound_control_edges(graph, target)
        iteration = self._join_iteration(run, target)
        entry = self._join_entry(run, target, iteration)
        resolved = entry["resolved"]
        if not all(edge.edge_id in resolved for edge in inbound):
            return
        delivered_edges = [edge for edge in inbound if resolved.get(edge.edge_id) == "delivered"]
        self._clear_join_entry(run, target, iteration)
        if delivered_edges:
            payloads = [entry["payloads"][edge.edge_id] for edge in delivered_edges]
            merged = self._merge_join_payloads(graph, target, payloads)
            self._stash_join_payload(run, target, merged)
            run.pending_node_ids.append(target)
        else:
            for out_edge in self._outbound_control_edges(graph, target):
                worklist.append((out_edge, False, None))

    def _merge_join_payloads(
        self,
        graph: Graph,
        node_id: str,
        payloads: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Combine delivered inbound payloads for a convergent node (B9).

        Reuses the parallel subsystem's ``dispatch_strategy`` reducer registry —
        the join barrier does not reinvent merge semantics. A single delivered
        payload is returned as-is (no merge invoked). Non-dict merge results
        (e.g. ``collect`` returns a list) are wrapped ``{"result": ...}`` to keep
        the downstream node payload a dict, matching the subgraph convention.
        """
        if len(payloads) == 1:
            return dict(payloads[0])
        node = node_by_id(graph, node_id)
        join_config = getattr(node, "join_config", None)
        if join_config is None:
            # Validation requires a JoinConfig for genuine concurrent delivery;
            # default to shallow merge as a safe fallback for unvalidated graphs.
            strategy, reducer_ref = "merge", None
        else:
            strategy, reducer_ref = join_config.merge_strategy, join_config.reducer_ref
        merged = dispatch_strategy(strategy, list(payloads), reducer_ref=reducer_ref)
        if not isinstance(merged, dict):
            return {"result": merged}
        return dict(merged)

    def _join_iteration(self, run: Run, node_id: str) -> str:
        """Iteration key for a node's join scope (stringified for JSON round-trip).

        DAG scope: a node is visited at most once, so this is always ``"0"``.
        Convergent-on-cycle nodes are rejected at publish validation under the
        flag, so per-iteration loop re-scoping (design §4.4) is deferred; this
        key intentionally stays ``"0"`` for every supported graph.
        """
        return str(run.node_visit_counts.get(node_id, 0))

    def _join_entry(self, run: Run, node_id: str, iteration: str) -> dict[str, Any]:
        """Get (creating if needed) the join-tracking entry for (node, iteration).

        Lives under ``run.metadata['join_state']`` which round-trips through the
        RunRepository checkpoint exactly like ``node_payloads`` — so a run paused
        mid-join resumes with its partial resolution intact.
        """
        join_state: dict[str, Any] = run.metadata.setdefault("join_state", {})
        node_state: dict[str, Any] = join_state.setdefault(node_id, {})
        entry: dict[str, Any] = node_state.setdefault(iteration, {"resolved": {}, "payloads": {}})
        return entry

    def _record_edge_resolution(
        self,
        run: Run,
        target: str,
        edge_id: str,
        delivered: bool,
        payload: dict[str, Any] | None,
    ) -> None:
        """Record one inbound edge of ``target`` as delivered (with payload) or suppressed."""
        iteration = self._join_iteration(run, target)
        entry = self._join_entry(run, target, iteration)
        entry["resolved"][edge_id] = "delivered" if delivered else "suppressed"
        if delivered and payload is not None:
            entry["payloads"][edge_id] = dict(payload)

    def _clear_join_entry(self, run: Run, node_id: str, iteration: str) -> None:
        """Drop a resolved join entry so a later loop iteration starts clean."""
        join_state = run.metadata.get("join_state", {})
        node_state = join_state.get(node_id)
        if node_state is None:
            return
        node_state.pop(iteration, None)
        if not node_state:
            join_state.pop(node_id, None)

    def _stash_join_payload(self, run: Run, node_id: str, payload: dict[str, Any]) -> None:
        """Store the input payload for a node about to be enqueued (mirrors queue_next_nodes)."""
        payloads = dict(run.metadata.get("node_payloads", {}))
        payloads[node_id] = dict(payload)
        run.metadata["node_payloads"] = payloads

    def _inbound_control_edges(self, graph: Graph, node_id: str) -> list[Edge]:
        """Non-tool inbound edges of a node, in graph-declaration order.

        Includes disabled edges: the branch planner resolves a disabled edge as
        SUPPRESSED, so the inbound set must count it or the join would wait for an
        edge that already resolved.
        """
        return [
            edge for edge in graph.edges if edge.target_node_id == node_id and edge.kind != "tool"
        ]

    def _outbound_control_edges(self, graph: Graph, node_id: str) -> list[Edge]:
        """Non-tool outbound edges of a node, in graph-declaration order."""
        return [
            edge for edge in graph.edges if edge.source_node_id == node_id and edge.kind != "tool"
        ]

    def increment_node_visit(self, run: Run, node_id: str) -> None:
        """Bump the visit counter for this node by one."""
        run.node_visit_counts[node_id] = run.node_visit_counts.get(node_id, 0) + 1

    def payload_for(self, run: Run, node_id: str) -> dict[str, Any]:
        """Get and remove the queued input payload for a node.

        Returns an empty dict if no payload was queued for this node.
        """
        payloads = dict(run.metadata.get("node_payloads", {}))
        payload = payloads.pop(node_id, None)
        run.metadata["node_payloads"] = payloads
        if payload is None:
            return {}
        return dict(payload)

    async def fail_run(self, run: Run, reason: str, message: str) -> Run:
        """Mark a run as failed with the given reason and save it.

        The ``message`` is routed through the secret redactor (audit S6): call
        sites pass ``str(exc)`` from node dispatch, whose exception text can echo
        a Vault-resolved token (httpx errors include the request URL/headers), and
        ``RunFailureState.message`` is returned verbatim by the public run API.
        ``redact`` is a no-op when no secret resolver is configured.
        """
        run.status = RunStatus.FAILED
        run.failure_state = RunFailureState(
            reason=reason, message=self.audit_recorder.redact(message)
        )
        run.metadata["termination_reason"] = reason
        run.touch()
        persisted = await self.run_repository.put(run)
        await self.run_repository.write_checkpoint(persisted)
        await self.refresh_artifact_ttls(persisted)
        await self.emit_webhook(
            "run.failed",
            persisted,
            {
                "run_id": persisted.run_id,
                "graph_version_ref": persisted.graph_version_ref,
                "status": "failed",
                "failure_reason": reason,
            },
        )
        return persisted

    async def emit_webhook(
        self,
        event_type: str,
        run: Run,
        data: dict[str, Any],
    ) -> None:
        """Emit a webhook event if a webhook service is configured."""
        ws = self.webhook_service
        if ws is None:
            return
        try:
            await ws.emit_event(
                event_type=event_type,
                deployment_ref=run.deployment_ref,
                tenant_id=run.tenant_id,
                data=data,
            )
        except Exception:
            logger.exception("failed to emit %s webhook", event_type)

    def entry_step(self, graph: Graph) -> str:
        """Get the ID of the first node to run in the graph."""
        if graph.entry_step is not None:
            return graph.entry_step
        if not graph.nodes:
            raise OrchestratorError("graph has no nodes")
        return graph.nodes[0].node_id

    def graph_version_ref(self, graph: Graph) -> str:
        """Build a version reference string like 'my-graph:v2'."""
        return f"{graph.graph_id}:v{graph.version}"

    def initial_metadata(self, graph: Graph, initial_input: Mapping[str, Any]) -> dict[str, Any]:
        """Build the starting metadata dict for a new run."""
        return {
            "graph_id": graph.graph_id,
            "graph_name": graph.name,
            "node_payloads": {self.entry_step(graph): dict(initial_input)},
            "edge_visit_counts": {},
            "path": [],
            "audits": {},
        }

    def edge_for(self, graph: Graph, source_node_id: str, target_node_id: str):
        """Find the data edge connecting two nodes, or None if there isn't one.

        Tool edges never carry mappings or route payloads, so they are
        skipped even when they connect the same pair of nodes.
        """
        for edge in graph.edges:
            if (
                edge.kind != "tool"
                and edge.source_node_id == source_node_id
                and edge.target_node_id == target_node_id
            ):
                return edge
        return None
