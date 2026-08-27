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
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from zeroth.contracts.conditions import NextStepPlanner
from zeroth.contracts.conditions.models import ConditionContext, NextStepPlan, TraversalState
from zeroth.contracts.governed import RunStatus
from zeroth.contracts.graph import Edge, Graph, HumanApprovalNode, SubgraphNode
from zeroth.contracts.graph.engine_mode import token_engine_enabled
from zeroth.contracts.mappings import MappingExecutor
from zeroth.contracts.mappings.executor import _set_path
from zeroth.platform.observability import start_span
from zeroth.runtime.agents.errors import BudgetExceededError
from zeroth.runtime.orchestration import token_scope as _ts
from zeroth.runtime.orchestration.audit_recorder import RuntimeAuditRecorder
from zeroth.runtime.orchestration.dispatcher import (
    NodeDispatcher,
    SideEffectReconciliationExhaustedError,
)
from zeroth.runtime.orchestration.errors import OrchestratorError
from zeroth.runtime.orchestration.parallel_executor import (
    RuntimeParallelExecutor,
    sum_run_cost,
)
from zeroth.runtime.orchestration.policy_gate import RuntimePolicyGate
from zeroth.runtime.orchestration.token_snapshot_store import TokenSnapshotStore
from zeroth.runtime.orchestration.tool_executor import node_by_id
from zeroth.runtime.parallel.errors import FanOutValidationError, ParallelExecutionError
from zeroth.runtime.parallel.models import GlobalStepTracker
from zeroth.runtime.parallel.reducers import dispatch_strategy
from zeroth.runtime.runs import Run, RunFailureState
from zeroth.runtime.runs.costs import rollup_cost_history, rollup_run_cost
from zeroth.runtime.subgraphs.errors import (
    SubgraphCycleError,
    SubgraphDepthLimitError,
    SubgraphExecutionError,
    SubgraphResolutionError,
)
from zeroth.runtime.subgraphs.resolver import merge_governance, namespace_subgraph

logger = logging.getLogger(__name__)


def _ts_tag_to_json(tag: _ts.TokenTag) -> list[list[Any]]:
    """Serialize a provenance tag to a JSON-safe ``[[header, iter], ...]`` list.

    Tags live in ``run.metadata`` (``node_tags`` and each join entry's ``tag``),
    which round-trips through the RunRepository checkpoint, so they must be plain
    JSON — tuples are not.
    """
    return [[header, iteration] for header, iteration in tag]


def _ts_tag_from_json(raw: Any) -> _ts.TokenTag:
    """Rebuild a provenance tag from its checkpointed JSON form."""
    if not raw:
        return _ts.INITIAL_TAG
    return tuple((header, int(iteration)) for header, iteration in raw)


@dataclass(frozen=True, slots=True)
class GraphDriver:
    """Drives a run through its graph, one node at a time, to a terminal state."""

    run_repository: Any
    token_snapshot_store: TokenSnapshotStore | None = None
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
    # B9 loop-scoping caches, keyed by (graph_id, version). The OWNER of these
    # dicts must be the long-lived orchestrator (the facade passes its own dicts
    # in), because graph ids/versions are only unique within one orchestrator's
    # lifetime — a global cache would poison a different topology that reuses
    # the same id (exactly what test suites do).
    back_edge_cache: dict[tuple[str, int], frozenset[str]] = field(default_factory=dict)
    scopes_cache: dict[tuple[str, int], _ts.GraphScopes] = field(default_factory=dict)

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

    async def _put_running(self, run: Run) -> Run:
        """Persist one drive transition only while the run is still RUNNING."""
        return await self.run_repository.put_if_status(run, RunStatus.RUNNING)

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
            try:
                persisted = await self.run_repository.put_if_status(run, fresh.status)
            except ValueError:
                latest = await self.run_repository.get(run.run_id)
                if latest is None:
                    raise
                return latest
            await self.run_repository.write_checkpoint(persisted)
            return persisted
        return None

    async def restore_in_flight_dispatch(self, graph: Graph, run: Run) -> None:
        """Re-stage a dispatch left durable by a failed node execution.

        The record is intentionally retained until the replayed node completes.
        That makes a second failure replayable too, and prevents the restore itself
        from creating a gap where the exact input exists only in memory.
        """
        raw = run.metadata.get("in_flight_dispatch")
        if raw is None:
            return
        if not isinstance(raw, Mapping):
            raise OrchestratorError("in-flight dispatch metadata is not a mapping")
        node_id = raw.get("node_id")
        input_payload = raw.get("input_payload")
        token_tag = raw.get("token_tag")
        if not isinstance(node_id, str) or not isinstance(input_payload, Mapping):
            raise OrchestratorError("in-flight dispatch metadata is missing node_id or payload")
        node = node_by_id(graph, node_id)
        if getattr(node, "parallel_config", None) is not None:
            raise OrchestratorError(
                f"in-flight marker targets parallel fan-out node {node_id}; "
                "ordinary failed-dispatch replay cannot restore fan-out state"
            )
        if node_id in run.pending_node_ids:
            raise OrchestratorError(
                f"in-flight node {node_id} is already pending; refusing duplicate replay"
            )
        payloads = dict(run.metadata.get("node_payloads", {}))
        existing_payload = payloads.get(node_id)
        if existing_payload is not None and existing_payload != input_payload:
            raise OrchestratorError(f"in-flight node {node_id} has a conflicting staged payload")
        if existing_payload is not None:
            raise OrchestratorError(f"in-flight node {node_id} already has staged payload state")
        payloads[node_id] = dict(input_payload)
        run.metadata["node_payloads"] = payloads
        tags = dict(run.metadata.get("node_tags", {}))
        existing_tag = tags.get(node_id)
        if token_engine_enabled(graph.execution_settings):
            if token_tag is None:
                raise OrchestratorError(
                    f"in-flight token node {node_id} is missing its provenance tag"
                )
            if existing_tag is not None and existing_tag != token_tag:
                raise OrchestratorError(
                    f"in-flight node {node_id} has a conflicting staged token tag"
                )
            tags[node_id] = token_tag
            run.metadata["node_tags"] = tags
        elif token_tag is not None:
            raise OrchestratorError(
                f"legacy in-flight node {node_id} unexpectedly carries a token tag"
            )
        run.pending_node_ids.insert(0, node_id)
        run.current_node_ids = []
        run.current_step = None

    async def stage_in_flight_dispatch(
        self,
        graph: Graph,
        run: Run,
        node_id: str,
        input_payload: Mapping[str, Any],
    ) -> None:
        """Durably bind a popped node to its exact payload and provenance tag."""
        token_tag: Any = None
        if token_engine_enabled(graph.execution_settings):
            tags = run.metadata.get("node_tags", {})
            if node_id not in tags:
                raise OrchestratorError(
                    f"token node {node_id} has no staged provenance tag at dispatch"
                )
            token_tag = tags[node_id]
        record = {
            "node_id": node_id,
            "input_payload": dict(input_payload),
            "token_tag": token_tag,
        }
        existing = run.metadata.get("in_flight_dispatch")
        if existing is not None and existing != record:
            raise OrchestratorError(
                f"cannot stage node {node_id}; another inconsistent dispatch is in flight"
            )
        run.metadata["in_flight_dispatch"] = record
        run.touch()
        persisted = await self._put_running(run)
        await self.run_repository.write_checkpoint(persisted)

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
        if token_engine_enabled(graph.execution_settings):
            if self.token_snapshot_store is None:
                raise RuntimeError("sequential_join_enabled requires a durable TokenSnapshotStore")
            from zeroth.runtime.orchestration.token_runtime import TokenRuntimeCoordinator

            return await TokenRuntimeCoordinator(self, self.token_snapshot_store).drive(
                graph, run, step_tracker=step_tracker
            )
        started_at = perf_counter()
        await self.restore_in_flight_dispatch(graph, run)
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
                persisted = await self._put_running(run)
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
            run = await self._put_running(run)

            # D-11 literal: resume path for a parallel fan-out that was
            # paused due to an approval inside a subgraph branch.
            pending_psg = run.metadata.get("pending_parallel_subgraph")
            if pending_psg and pending_psg.get("node_id") == node_id:
                try:
                    fan_in_resume = await self.parallel_runtime.execute_fan_out_resume(
                        graph,
                        run,
                        node,
                        node_id,
                        pending_psg,
                        step_tracker=step_tracker,
                    )
                except Exception as exc:
                    source_output = pending_psg.get("split_input", dict(input_payload))
                    await self.audit_recorder.record_history(
                        run,
                        node,
                        node_id,
                        pending_psg.get("source_input", input_payload),
                        source_output,
                        pending_psg.get("source_audit") or {"resumed_parallel_fan_out": True},
                        started_at=node_started_at,
                    )
                    self.increment_node_visit(run, node_id)
                    del run.metadata["pending_parallel_subgraph"]
                    return await self.fail_run(run, "parallel_execution_failed", str(exc))
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
                # The initial (paused) attempt short-circuited BEFORE the main
                # path records the fan-out source node's own history and bumps its
                # visit count, so do both now on resume (audit re-review #5). The
                # source node's own output is the split input (what was fanned
                # out); use it — not the fan-IN merged output — to plan the fan-out
                # node's downstream edges, so their conditions evaluate on the
                # right payload, exactly as the main path does (audit re-review #6).
                source_output = pending_psg.get("split_input", dict(input_payload))
                await self.audit_recorder.record_history(
                    run,
                    node,
                    node_id,
                    pending_psg.get("source_input", input_payload),
                    source_output,
                    pending_psg.get("source_audit") or {"resumed_parallel_fan_out": True},
                    started_at=node_started_at,
                )
                self.increment_node_visit(run, node_id)
                # Merge branch state and continue post-fan-in flow.
                self.parallel_runtime.merge_fan_in_state(run, fan_in_resume)
                merged_output = fan_in_resume.merged_output
                downstream_ids = self.plan_next_nodes(graph, run, node_id, source_output)
                for ds_id in downstream_ids:
                    self.increment_node_visit(run, ds_id)
                    # Route the post-fan-in hop through the SAME dispatch entry
                    # point as every other node completion so a convergent node
                    # reached through a fan-out enters the join barrier instead of
                    # the legacy last-writer-wins queue (B9 audit #3/#4/#5). Under
                    # the flag off this is byte-identical to the old
                    # plan_next_nodes + queue_next_nodes pair (both funnel through
                    # run_branch_planner); the increment_node_visit above stays
                    # load-bearing because advance_downstream does not bump it.
                    self.advance_downstream(graph, run, ds_id, merged_output)
                run.metadata["last_output"] = merged_output
                # Cooperative cancel across a resumed fan-in (audit F3 follow-up).
                stopped = await self.external_stop(run)
                if stopped is not None:
                    return stopped
                run.touch()
                run = await self._put_running(run)
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
                run.status = RunStatus.WAITING_APPROVAL
                # Put the same node back at the front so execution can resume from this gate.
                run.metadata["pending_approval"] = {
                    "node_id": node.node_id,
                    "input": input_payload,
                    "approval_id": approval_id,
                }
                run.pending_node_ids.insert(0, node.node_id)
                run.touch()
                persisted = await self._put_running(run)
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
                    child_cost = rollup_cost_history(child_run.execution_history)
                    child_run.metadata.update(
                        total_cost_usd=child_cost.cost_usd,
                        total_estimated_cost_usd=child_cost.estimated_cost_usd,
                        cost_measurement=child_cost.cost_measurement,
                    )

                    if child_run.status == RunStatus.WAITING_APPROVAL:
                        # Still waiting (nested approval or another gate in subgraph).
                        # Stay paused -- pending_subgraph metadata already correct.
                        run.status = RunStatus.WAITING_APPROVAL
                        run.pending_node_ids.insert(0, node_id)
                        run.touch()
                        persisted = await self._put_running(run)
                        await self.run_repository.write_checkpoint(persisted)
                        await self.refresh_artifact_ttls(persisted)
                        return persisted

                    if child_run.status != RunStatus.COMPLETED:
                        failure = child_run.failure_state
                        detail = failure.message if failure is not None else "unknown failure"
                        error = SubgraphExecutionError(
                            f"child run {child_run.run_id} ended "
                            f"{child_run.status.value}: {detail}",
                        )
                        error.audit_record = {  # type: ignore[attr-defined]
                            "subgraph_run_id": child_run.run_id,
                            "subgraph_graph_ref": graph_ref,
                            "subgraph_status": child_run.status.value,
                            "subgraph_resumed": True,
                            "cost_usd": child_cost.cost_usd,
                            "estimated_cost_usd": child_cost.estimated_cost_usd,
                            "cost_measurement": child_cost.cost_measurement,
                        }
                        await self.audit_recorder.record_failed_execution(
                            run,
                            node,
                            node_id,
                            input_payload,
                            error,
                            started_at=node_started_at,
                        )
                        return await self.fail_run(run, "subgraph_execution_failed", str(error))

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
                        "cost_usd": child_run.metadata.get("total_cost_usd"),
                        "estimated_cost_usd": child_run.metadata.get("total_estimated_cost_usd"),
                        "cost_measurement": child_run.metadata.get("cost_measurement"),
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
                    persisted = await self._put_running(run)
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
                    await self.audit_recorder.record_failed_execution(
                        run,
                        node,
                        node_id,
                        input_payload,
                        exc,
                        started_at=node_started_at,
                    )
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
                    persisted = await self._put_running(run)
                    await self.run_repository.write_checkpoint(persisted)
                    await self.refresh_artifact_ttls(persisted)
                    return persisted

                child_cost = rollup_run_cost(child_run)
                if child_run.status != RunStatus.COMPLETED:
                    failure = child_run.failure_state
                    detail = failure.message if failure is not None else "unknown failure"
                    error = SubgraphExecutionError(
                        f"child run {child_run.run_id} ended {child_run.status.value}: {detail}",
                    )
                    error.audit_record = {  # type: ignore[attr-defined]
                        "subgraph_run_id": child_run.run_id,
                        "subgraph_graph_ref": node.subgraph.graph_ref,
                        "subgraph_status": child_run.status.value,
                        "subgraph_depth": child_run.metadata.get("subgraph_depth", 0),
                        "cost_usd": child_cost.cost_usd,
                        "estimated_cost_usd": child_cost.estimated_cost_usd,
                        "cost_measurement": child_cost.cost_measurement,
                    }
                    await self.audit_recorder.record_failed_execution(
                        run,
                        node,
                        node_id,
                        input_payload,
                        error,
                        started_at=node_started_at,
                    )
                    return await self.fail_run(run, "subgraph_execution_failed", str(error))

                # Use child run's final_output as this node's output.
                output_data = child_run.final_output or {}
                if not isinstance(output_data, dict):
                    output_data = {"result": output_data}

                audit_record = {
                    "subgraph_run_id": child_run.run_id,
                    "subgraph_graph_ref": node.subgraph.graph_ref,
                    "subgraph_status": child_run.status.value,
                    "subgraph_depth": child_run.metadata.get("subgraph_depth", 0),
                    "cost_usd": child_run.metadata.get("total_cost_usd"),
                    "estimated_cost_usd": child_run.metadata.get("total_estimated_cost_usd"),
                    "cost_measurement": child_run.metadata.get("cost_measurement"),
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
                persisted = await self._put_running(run)
                await self.run_repository.write_checkpoint(persisted)
                await self.refresh_artifact_ttls(persisted)
                continue

            try:
                # Per-run cost ceiling (local, control-plane-independent). Post-hoc:
                # cumulative spend from prior nodes is read from the run's own audit
                # cost_usd, so the run halts on the NEXT node once it crosses the cap.
                if self.per_run_cap_usd is not None:
                    spent = sum_run_cost(run)
                    if spent is None:
                        raise BudgetExceededError(
                            "per-run budget cannot be evaluated: cost is unmeasured",
                            cap=self.per_run_cap_usd,
                        )
                    if spent >= self.per_run_cap_usd:
                        raise BudgetExceededError(
                            f"per-run budget exceeded: ${spent:.4f} >= ${self.per_run_cap_usd:.4f}",
                            spend=spent,
                            cap=self.per_run_cap_usd,
                        )
                if getattr(node, "parallel_config", None) is None:
                    await self.stage_in_flight_dispatch(graph, run, node_id, input_payload)
                output_data, audit_record = await self.node_dispatcher.dispatch(
                    node, run, input_payload, graph
                )
            except Exception as exc:
                # One clause, with the terminal-vs-resumable decision in the
                # helper: `drive` is already at the complexity ceiling the
                # commit gate enforces, and a second except clause pushes it
                # over without making the branch any clearer.
                return await self._settle_failed_dispatch(
                    run, node, node_id, input_payload, exc, node_started_at
                )

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
                    # Route the post-fan-in hop through the SAME dispatch entry
                    # point as every other node completion so a convergent node
                    # reached through a fan-out enters the join barrier instead of
                    # the legacy last-writer-wins queue (B9 audit #3/#4/#5). Under
                    # the flag off this is byte-identical to the old
                    # plan_next_nodes + queue_next_nodes pair (both funnel through
                    # run_branch_planner); the increment_node_visit above stays
                    # load-bearing because advance_downstream does not bump it.
                    self.advance_downstream(graph, run, ds_id, merged_output)
                run.metadata["last_output"] = merged_output
                # Cooperative cancel across a parallel fan-in (audit F3 follow-up).
                stopped = await self.external_stop(run)
                if stopped is not None:
                    return stopped
                run.status = RunStatus.RUNNING
                run.current_node_ids = []
                run.touch()
                run = await self._put_running(run)
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
            run.metadata.pop("in_flight_dispatch", None)
            # An operator may have cancelled/interrupted while this node was
            # dispatching; don't clobber that with our RUNNING write (audit F3).
            stopped = await self.external_stop(run)
            if stopped is not None:
                return stopped
            run.status = RunStatus.RUNNING
            run.current_node_ids = []
            run.touch()
            run = await self._put_running(run)
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
        With explicit ``sequential_join_enabled=False`` this is byte-identical
        to the historical ``plan_next_nodes`` + ``queue_next_nodes`` pairing.
        Token mode routes through the join engine: each edge carries a
        provenance tag that TRAVELS WITH THE TOKEN, a convergent node's join is
        keyed by that tag (so it re-joins cleanly on every loop iteration), and a
        loop-exit edge resolves only when its loop terminates.
        """
        plan = self.run_branch_planner(graph, run, source_node_id, output_data)
        if not token_engine_enabled(graph.execution_settings):
            self.queue_next_nodes(graph, run, source_node_id, output_data, list(plan.next_node_ids))
            return
        source_tag = self._consume_node_tag(run, source_node_id)
        self._resolve_join_edges(graph, run, source_node_id, output_data, plan, source_tag)

    def _resolve_join_edges(
        self,
        graph: Graph,
        run: Run,
        source_node_id: str,
        output_data: Mapping[str, Any],
        plan: NextStepPlan,
        source_tag: _ts.TokenTag,
    ) -> None:
        """Seed the token-join worklist from one source node's outgoing edges.

        Every non-tool outgoing edge resolves as DELIVERED (active — carries the
        mapped payload) or SUPPRESSED, and carries the provenance TAG the token
        holds after crossing it (:func:`token_scope.propagate_tag`). Draining the
        worklist dispatches any now-fully-resolved convergent target (keyed by
        that tag) and propagates the skip cascade.

        **Loop-exit edges are special (P3).** A loop-exit edge is NEVER resolved by
        its source node's ordinary edge resolution — only by the *exit-crossing
        event*: when the source takes an ACTIVE exit edge of a loop L, L
        terminates and L's whole exit-edge unit resolves at once at the outer tag
        (the crossed edge delivers, its siblings suppress). That is what lets an
        out-of-loop join see exactly one resolution of each of L's exit edges — at
        the right tag, when the loop is actually done — with no per-iteration
        premature dispatch and no hang. Exit edges of loops the source is in but
        did NOT cross this pass are deferred: they resolve later, when their own
        owning loop terminates.
        """
        scopes = self._graph_scopes(graph)
        resolution = plan.branch_resolution
        edge_map = {edge.edge_id: edge for edge in graph.edges}
        active_ids = [
            eid
            for eid in resolution.active_edge_ids
            if (e := edge_map.get(eid)) is not None and e.kind != "tool"
        ]
        suppressed_ids = [
            eid
            for eid in resolution.suppressed_edge_ids
            if (e := edge_map.get(eid)) is not None and e.kind != "tool"
        ]
        active_set = set(active_ids)
        # A loop is CROSSED this pass iff the source took an ACTIVE edge that EXITS
        # it. A single edge may exit several nested loops at once, so crossing is
        # recorded for EVERY loop an active exit edge leaves — not just its
        # outermost owner — otherwise an inner loop the token also bailed out of
        # would never terminate. Crossing a set of loops resolves, as one event,
        # every exit edge OWNED by (outermost-exits) any crossed loop; each such
        # edge whose owner is not yet crossed stays deferred. Exit edges among the
        # source's OWN edges are withheld from the normal resolution below; only
        # crossed units fire, in (B).
        crossed_loops: set[str] = set()
        for eid in active_ids:
            if eid in scopes.exit_owner:
                crossed_loops |= {
                    header for header, eids in scopes.exit_edges.items() if eid in eids
                }
        unit_edge_ids = {eid for eid, owner in scopes.exit_owner.items() if owner in crossed_loops}
        source_exit_ids = {
            eid for eid in (*active_ids, *suppressed_ids) if eid in scopes.exit_owner
        }
        worklist: deque[tuple[Edge, bool, dict[str, Any] | None, _ts.TokenTag]] = deque()
        # (A) Normal (non-exit) edges — deliver/suppress at the propagated tag.
        for eid in active_ids:
            if eid in source_exit_ids:
                continue
            edge = edge_map[eid]
            tag = _ts.propagate_tag(source_tag, edge, scopes)
            payload = self.edge_payload(
                graph, run, source_node_id, edge.target_node_id, output_data, edge
            )
            worklist.append((edge, True, payload, tag))
        for eid in suppressed_ids:
            if eid in source_exit_ids:
                continue
            edge = edge_map[eid]
            if eid in scopes.back_edges:
                # A suppressed back-edge is the loop declining to iterate via this
                # edge; termination is handled by the exit-crossing event, so this
                # is a no-op (recording it would spuriously feed the header's join).
                continue
            tag = _ts.propagate_tag(source_tag, edge, scopes)
            worklist.append((edge, False, None, tag))
        # (B) Exit-crossing unit resolution — the ONLY place exit edges resolve.
        # Each edge resolves at the tag a token arriving at its TARGET would carry
        # (``propagate_tag``): it strips the loops the edge exits AND adds a fresh
        # (header, 0) for any loop the target ENTERS — so an exit edge whose target
        # is itself a loop header lands in the same bucket as that node's other
        # inbound, and a sibling exit that leaves fewer loops lands at the target's
        # own scope. A strip-only tag would key these into divergent buckets that
        # never complete.
        for eid in unit_edge_ids:
            edge = edge_map[eid]
            tag = _ts.propagate_tag(source_tag, edge, scopes)
            if eid in active_set:
                payload = self.edge_payload(
                    graph, run, source_node_id, edge.target_node_id, output_data, edge
                )
                worklist.append((edge, True, payload, tag))
            else:
                worklist.append((edge, False, None, tag))
        self._drain_join_worklist(graph, run, worklist, scopes)

    def _drain_join_worklist(
        self,
        graph: Graph,
        run: Run,
        worklist: deque[tuple[Edge, bool, dict[str, Any] | None, _ts.TokenTag]],
        scopes: _ts.GraphScopes,
    ) -> None:
        """Resolve edges until the worklist drains, enqueuing ready join targets.

        A ``parallel_config`` target owns its own fan-in, so it bypasses the
        barrier (queued directly on delivery, exactly like the legacy path).

        Each (edge, tag) resolves at most once per drain: a skip cascade walking a
        cycle can re-reach an edge at the same tag; first resolution wins.

        **Forward vs back edge.** A FORWARD edge feeds its target's join bucket for
        the token's tag. A delivered BACK edge is a loop re-entry: it dispatches
        the header once at the bumped tag (the tag already carries the incremented
        iteration); a suppressed back-edge is a no-op (see ``_resolve_join_edges``).
        """
        visited: set[tuple[str, str]] = set()
        back_dispatched: set[tuple[str, str]] = set()
        while worklist:
            edge, delivered, payload, tag = worklist.popleft()
            tkey = _ts.tag_key(tag)
            if (edge.edge_id, tkey) in visited:
                continue
            visited.add((edge.edge_id, tkey))
            target = edge.target_node_id
            if edge.edge_id in scopes.back_edges:
                # Loop re-entry. Dispatch the header once per (header, tag) even if
                # two back-edges into one header deliver together.
                if delivered and payload is not None and (target, tkey) not in back_dispatched:
                    back_dispatched.add((target, tkey))
                    self._stash_join_payload(run, target, payload, tag)
                    run.pending_node_ids.append(target)
                continue
            self._record_forward_resolution(run, target, edge.edge_id, delivered, payload, tag)
            self._check_forward_join(graph, run, target, tag, worklist, scopes)

    def _check_forward_join(
        self,
        graph: Graph,
        run: Run,
        target: str,
        tag: _ts.TokenTag,
        worklist: deque[tuple[Edge, bool, dict[str, Any] | None, _ts.TokenTag]],
        scopes: _ts.GraphScopes,
    ) -> None:
        """Dispatch or skip ``target`` once all its FORWARD inbound resolve at ``tag``.

        The join bucket is keyed by the token's provenance tag, so a convergent
        node re-joins cleanly on every loop iteration (each iteration is a distinct
        tag) — including an inner loop re-entered by an outer one, where the
        counter-epoch model wrongly deadlocked.

        - all forward inbound resolved at this tag, >=1 delivered → merge, dispatch
          once with this tag;
        - all resolved, none delivered → the node was not entered at this tag: SKIP
          it, cascading its FORWARD, non-exit outbound as SUPPRESSED so downstream
          joins learn the branch is resolved-not-pending;
        - otherwise keep waiting.

        Back-edges are not in the forward set, so a loop header never waits on a
        back-edge that cannot fire yet.
        """
        forward = self._forward_inbound_edges(graph, target)
        if not forward:
            # Only back-edge inbound (a pure loop header) or the entry node —
            # dispatched via the back-edge path or the initial seed, not here.
            return
        entry = self._join_entry(run, target, tag)
        resolved = entry["resolved"]
        if not all(edge.edge_id in resolved for edge in forward):
            return
        delivered_edges = [edge for edge in forward if resolved.get(edge.edge_id) == "delivered"]
        self._clear_join_entry(run, target, tag)
        if delivered_edges:
            payloads = [entry["payloads"][edge.edge_id] for edge in delivered_edges]
            merged = self._merge_join_payloads(graph, target, payloads)
            self._stash_join_payload(run, target, merged, tag)
            run.pending_node_ids.append(target)
        else:
            # SKIP: cascade FORWARD, non-exit outbound as suppressed. Exit edges
            # resolve ONLY via the exit-crossing unit (never a cascade, which would
            # re-resolve them at the still-in-loop tag and orphan the target); a
            # back-edge is a loop-exit no-op.
            for out_edge in self._outbound_control_edges(graph, target):
                if out_edge.edge_id in scopes.back_edges:
                    continue
                if out_edge.edge_id in scopes.exit_owner:
                    continue
                out_tag = _ts.propagate_tag(tag, out_edge, scopes)
                worklist.append((out_edge, False, None, out_tag))
            # A skipped loop HEADER means the loop was never entered at this tag —
            # it is dead, so no token will ever cross its exit edges. Resolve that
            # loop's whole exit-edge unit as SUPPRESSED now (the cascade above
            # deliberately skips exit edges), each at its target's scope, or an
            # out-of-loop join waiting on a bypassed loop's exit hangs forever and
            # leaks its bucket. Only a HEADER skip kills the loop; a skipped body
            # node is a dead branch inside a still-live loop.
            if target in scopes.bodies:
                edge_map = {edge.edge_id: edge for edge in graph.edges}
                for eid, owner in scopes.exit_owner.items():
                    if owner != target:
                        continue
                    exit_edge = edge_map.get(eid)
                    if exit_edge is None:
                        continue
                    out_tag = _ts.propagate_tag(tag, exit_edge, scopes)
                    worklist.append((exit_edge, False, None, out_tag))

    def _merge_join_payloads(
        self,
        graph: Graph,
        node_id: str,
        payloads: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Route a join merge through the facade seam when present.

        See :meth:`_record_forward_resolution` for the pattern.
        """
        if self.orchestrator is not None and hasattr(self.orchestrator, "_merge_join_payloads"):
            return self.orchestrator._merge_join_payloads(graph, node_id, payloads)
        return self.merge_join_payloads(graph, node_id, payloads)

    def merge_join_payloads(
        self,
        graph: Graph,
        node_id: str,
        payloads: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Combine delivered inbound payloads for a convergent node (B9).

        Reuses the parallel subsystem's ``dispatch_strategy`` reducer registry —
        the join barrier does not reinvent merge semantics.

        The result shape follows the node's **declared** ``JoinConfig``, never the
        runtime payload count: a ``collect`` join yields a list even when only one
        inbound edge happened to deliver this iteration. Shape must not depend on
        which branch fired, or the same node would hand downstream a dict on one
        run and a list on the next. A node with no ``JoinConfig`` (conditional
        reconvergence — at most one delivery by construction) keeps its payload
        verbatim, byte-identical to the legacy single-payload enqueue.

        Non-dict reduced values (notably the ``collect`` list) are written at the
        config's ``merge_path``, mirroring how ``collect_fan_in`` writes its
        reduced value at ``split_path``; absent a ``merge_path`` they land under
        ``result``.
        """
        node = node_by_id(graph, node_id)
        join_config = getattr(node, "join_config", None)
        if join_config is None:
            if len(payloads) == 1:
                return dict(payloads[0])
            # Genuine concurrent delivery with no declared policy is rejected at
            # publish (MISSING_JOIN_CONFIG). Reaching here means a code-authored
            # graph that skipped validation: collect so no parent's data is lost.
            strategy, reducer_ref, merge_path = "collect", None, None
        else:
            strategy = join_config.merge_strategy
            reducer_ref = join_config.reducer_ref
            merge_path = join_config.merge_path
        merged = dispatch_strategy(strategy, list(payloads), reducer_ref=reducer_ref)
        if isinstance(merged, dict):
            return dict(merged)
        joined: dict[str, Any] = {}
        _set_path(joined, merge_path or "result", merged)
        return joined

    def _graph_scopes(self, graph: Graph) -> _ts.GraphScopes:
        """Static loop analysis (bodies, enclosing loops, exit edges + owners).

        Delegates to :func:`token_scope.analyze` and caches per immutable
        (graph_id, version). Replaces the old per-node loop-nesting cache; the
        token engine reads bodies/exit-edges/exit-owners off the one object.
        """
        key = (graph.graph_id, graph.version)
        cached = self.scopes_cache.get(key)
        if cached is None:
            cached = _ts.analyze(graph)
            self.scopes_cache[key] = cached
        return cached

    def _consume_node_tag(self, run: Run, node_id: str) -> _ts.TokenTag:
        """Read and clear the provenance tag a node was dispatched with.

        Parallels ``node_payloads``: whenever a payload is stashed for a node the
        token engine also stashes its tag (:meth:`_stash_join_payload`). Consumed
        here when the node runs, so the tag it carried becomes the base for
        propagating its own outgoing edges. A node with no recorded tag (e.g. one
        reached via the fan-out path, which the token engine does not yet tag)
        defaults to the outermost scope.
        """
        tags = dict(run.metadata.get("node_tags", {}))
        raw = tags.pop(node_id, None)
        run.metadata["node_tags"] = tags
        return _ts_tag_from_json(raw)

    def _join_entry(self, run: Run, node_id: str, tag: _ts.TokenTag) -> dict[str, Any]:
        """Get (creating if needed) the join bucket for ``(node_id, tag)``.

        Lives under ``run.metadata['join_state'][node_id][tag_key]`` and
        round-trips through the RunRepository checkpoint exactly like
        ``node_payloads`` — so a run paused mid-join resumes with its partial
        resolution intact. The bucket stores the tag itself so a completed join can
        re-stash its merged payload at the right scope.
        """
        join_state: dict[str, Any] = run.metadata.setdefault("join_state", {})
        node_state: dict[str, Any] = join_state.setdefault(node_id, {})
        key = _ts.tag_key(tag)
        entry = node_state.get(key)
        if entry is None:
            entry = {"tag": _ts_tag_to_json(tag), "resolved": {}, "payloads": {}}
            node_state[key] = entry
        return entry

    def _record_forward_resolution(
        self,
        run: Run,
        target: str,
        edge_id: str,
        delivered: bool,
        payload: dict[str, Any] | None,
        tag: _ts.TokenTag,
    ) -> None:
        """Route one forward-edge resolution through the facade seam when present.

        The monolith exposed ``_record_forward_resolution`` on the orchestrator
        and the trace/oracle bridge subclasses it to observe the token engine.
        With a facade attached the call goes through it (so overrides fire); the
        facade's base implementation calls straight back into
        :meth:`record_forward_resolution`, which is also the standalone path.
        """
        if self.orchestrator is not None and hasattr(
            self.orchestrator, "_record_forward_resolution"
        ):
            self.orchestrator._record_forward_resolution(
                run, target, edge_id, delivered, payload, tag
            )
            return
        self.record_forward_resolution(run, target, edge_id, delivered, payload, tag)

    def record_forward_resolution(
        self,
        run: Run,
        target: str,
        edge_id: str,
        delivered: bool,
        payload: dict[str, Any] | None,
        tag: _ts.TokenTag,
    ) -> None:
        """Record one FORWARD inbound edge of ``target`` in its ``tag`` join bucket."""
        entry = self._join_entry(run, target, tag)
        entry["resolved"][edge_id] = "delivered" if delivered else "suppressed"
        if delivered and payload is not None:
            entry["payloads"][edge_id] = dict(payload)

    def _clear_join_entry(self, run: Run, node_id: str, tag: _ts.TokenTag) -> None:
        """Drop a resolved join bucket so a later iteration's tag starts clean."""
        join_state = run.metadata.get("join_state", {})
        node_state = join_state.get(node_id)
        if node_state is None:
            return
        node_state.pop(_ts.tag_key(tag), None)
        if not node_state:
            join_state.pop(node_id, None)

    def _stash_join_payload(
        self, run: Run, node_id: str, payload: dict[str, Any], tag: _ts.TokenTag
    ) -> None:
        """Route a payload/tag staging through the facade seam when present.

        Mirrors :meth:`_record_forward_resolution` — the trace/oracle bridge
        observes dispatch stagings by overriding the facade method.
        """
        if self.orchestrator is not None and hasattr(self.orchestrator, "_stash_join_payload"):
            self.orchestrator._stash_join_payload(run, node_id, payload, tag)
            return
        self.stash_join_payload(run, node_id, payload, tag)

    def stash_join_payload(
        self, run: Run, node_id: str, payload: dict[str, Any], tag: _ts.TokenTag
    ) -> None:
        """Stage a node's input payload AND its provenance tag before enqueue.

        Mirrors ``queue_next_nodes`` for the payload, and additionally records the
        tag the node will run under (consumed by :meth:`_consume_node_tag`). A node
        already staged at a DIFFERENT tag means two live tokens want the same node
        at once — the single-circulating-token invariant the supported set
        guarantees (fan-out-in-loop is rejected at publish) is broken; fail loud
        rather than silently overwrite and complete with a wrong payload.
        """
        payloads = dict(run.metadata.get("node_payloads", {}))
        payloads[node_id] = dict(payload)
        run.metadata["node_payloads"] = payloads
        tags = dict(run.metadata.get("node_tags", {}))
        new_tag = _ts_tag_to_json(tag)
        existing = tags.get(node_id)
        if existing is not None and existing != new_tag:
            raise OrchestratorError(
                f"node {node_id} is already staged at tag {existing}; cannot re-stage "
                f"at {new_tag}. Two concurrent tokens share a node — an unsupported "
                "multi-token tag (fan-out inside a loop is rejected at publish)."
            )
        tags[node_id] = new_tag
        run.metadata["node_tags"] = tags

    def _back_edge_ids(self, graph: Graph) -> frozenset[str]:
        """Ids of the edges that close a cycle (DFS back-edges).

        An edge is a back-edge when its target is an ancestor of its source on
        the DFS stack — the edge that re-enters a loop. Classification is
        **structural**, deliberately not taken from
        ``Condition.allow_cycle_traversal``: that flag only gates whether the
        planner will traverse a *conditional* edge into a node already on the
        path, and an unconditional edge loops without it (``conditions/branch.py``
        only consults the flag when ``condition is not None``). A declarative flag
        therefore cannot identify which edges are loops; the topology can.

        Cached per (graph_id, version) — a published graph version is immutable.
        """
        key = (graph.graph_id, graph.version)
        cached = self.back_edge_cache.get(key)
        if cached is not None:
            return cached

        outgoing: dict[str, list[Edge]] = defaultdict(list)
        for edge in graph.edges:
            # Disabled edges route no control flow (token_scope._control_edges
            # drops them), so they must NOT be walked here either — otherwise this
            # DFS classifier and token_scope.back_edges disagree on which edges are
            # loops when a disabled edge is present, and a delivered edge gets left
            # out of a join's forward wait-set → false join_deadlock (review D2).
            if edge.kind != "tool" and edge.enabled:
                outgoing[edge.source_node_id].append(edge)

        on_stack, done = 0, 1
        back: set[str] = set()
        state: dict[str, int] = {}
        # Entry first so classification matches real traversal order, then every
        # remaining node so disconnected components are still classified.
        roots = [graph.entry_step] if graph.entry_step else []
        roots += [node.node_id for node in graph.nodes]
        for root in roots:
            if root is None or root in state:
                continue
            state[root] = on_stack
            stack: list[tuple[str, Iterator[Edge]]] = [(root, iter(outgoing[root]))]
            while stack:
                node_id, edge_iter = stack[-1]
                descended = False
                for edge in edge_iter:
                    target_state = state.get(edge.target_node_id)
                    if target_state == on_stack:
                        # Target is an ancestor of this node (or the node itself,
                        # for a self-loop) → this edge re-enters a loop.
                        back.add(edge.edge_id)
                    elif target_state is None:
                        state[edge.target_node_id] = on_stack
                        stack.append((edge.target_node_id, iter(outgoing[edge.target_node_id])))
                        descended = True
                        break
                if not descended:
                    state[node_id] = done
                    stack.pop()

        result = frozenset(back)
        self.back_edge_cache[key] = result
        return result

    def _forward_inbound_edges(self, graph: Graph, node_id: str) -> list[Edge]:
        """A node's FORWARD (non-back) inbound edges — the ones its join waits for.

        A plain AND-join over *all* inbound would deadlock any loop: a loop
        header's back-edge cannot deliver on the first visit (its source has not
        run yet). Back-edges are handled separately as loop re-entries, so the
        forward join waits only for the forward inbound.
        """
        back_ids = self._back_edge_ids(graph)
        return [
            edge
            for edge in self._inbound_control_edges(graph, node_id)
            if edge.edge_id not in back_ids
        ]

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
        expected_status = run.status
        run.status = RunStatus.FAILED
        run.failure_state = RunFailureState(
            reason=reason, message=self.audit_recorder.redact(message)
        )
        run.metadata["termination_reason"] = reason
        run.touch()
        persisted = await self.run_repository.put_if_status(run, expected_status)
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

    async def _settle_failed_dispatch(
        self,
        run: Run,
        node: Any,
        node_id: str,
        input_payload: Any,
        exc: BaseException,
        node_started_at: Any,
    ) -> Run:
        """Record the failed dispatch, then end the run the way it deserves.

        An exhausted ambiguous side effect is the one case that is *not* a
        failure: the effect may have landed and nobody can say, so the run pauses
        resumably instead of asserting it did not happen.
        """
        if isinstance(exc, SideEffectReconciliationExhaustedError):
            # Not a failed execution. Recording one and then pausing states two
            # contradictory things about the same node: that it failed, and that
            # it is waiting to be reconciled. Only the pause is true.
            await self.audit_recorder.record_history(
                run,
                node,
                node_id,
                input_payload,
                {},
                {
                    "reason_code": "side_effect_reconciliation_exhausted",
                    "operation_reconciliation_exhausted": True,
                    "operation_residual_duplicate_risk": True,
                },
                started_at=node_started_at,
            )
            return await self.pause_for_reconciliation(run, node_id, str(exc))
        await self.audit_recorder.record_failed_execution(
            run, node, node_id, input_payload, exc, started_at=node_started_at
        )
        return await self.fail_run(run, "node_execution_failed", str(exc))

    async def pause_for_reconciliation(self, run: Run, node_id: str, message: str) -> Run:
        """Pause a run whose side effect is ambiguous and out of retries.

        Failing here would be a claim the runtime cannot support: an exhausted
        ambiguous operation means nobody knows whether the effect landed, and
        ``FAILED`` asserts it did not. ``WAITING_INTERRUPT`` is the resumable
        state, so the durable reconciliation work can be settled out of band and
        the run continued rather than restarted.
        """
        expected_status = run.status
        run.status = RunStatus.WAITING_INTERRUPT
        # run_store rejects WAITING_INTERRUPT without an interrupt id, and the
        # operation key is the natural handle for the work that has to settle.
        run.pending_interrupt_id = f"reconcile:{node_id}"
        run.metadata["pending_reconciliation"] = {
            "node_id": node_id,
            "reason": self.audit_recorder.redact(message),
        }
        run.touch()
        persisted = await self.run_repository.put_if_status(run, expected_status)
        await self.run_repository.write_checkpoint(persisted)
        await self.emit_webhook(
            "run.waiting_interrupt",
            persisted,
            {
                "run_id": persisted.run_id,
                "graph_version_ref": persisted.graph_version_ref,
                "status": "waiting_interrupt",
                "reason": "side_effect_reconciliation_exhausted",
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
        entry = self.entry_step(graph)
        metadata: dict[str, Any] = {
            "graph_id": graph.graph_id,
            "graph_name": graph.name,
            "edge_visit_counts": {},
            "path": [],
            "audits": {},
        }
        if token_engine_enabled(graph.execution_settings):
            metadata["initial_input"] = dict(initial_input)
            # Empty compatibility containers preserve the public Run metadata
            # shape without using node-keyed state as scheduler storage.
            metadata["node_payloads"] = {}
            metadata["node_tags"] = {}
        else:
            metadata["node_payloads"] = {entry: dict(initial_input)}
        return metadata

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
