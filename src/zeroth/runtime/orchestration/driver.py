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
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from zeroth.contracts.conditions import NextStepPlanner
from zeroth.contracts.conditions.models import ConditionContext, TraversalState
from zeroth.contracts.mappings import MappingExecutor
from zeroth.core.agent_runtime.errors import BudgetExceededError
from zeroth.core.graph import Graph, HumanApprovalNode, SubgraphNode
from zeroth.core.parallel.errors import FanOutValidationError, ParallelExecutionError
from zeroth.core.parallel.models import GlobalStepTracker
from zeroth.core.runs import Run, RunFailureState, RunStatus
from zeroth.core.subgraph.errors import (
    SubgraphCycleError,
    SubgraphDepthLimitError,
    SubgraphExecutionError,
    SubgraphResolutionError,
)
from zeroth.core.subgraph.resolver import merge_governance, namespace_subgraph
from zeroth.platform.observability import start_span
from zeroth.runtime.orchestration.audit_recorder import RuntimeAuditRecorder
from zeroth.runtime.orchestration.dispatcher import NodeDispatcher
from zeroth.runtime.orchestration.errors import OrchestratorError
from zeroth.runtime.orchestration.parallel_executor import (
    RuntimeParallelExecutor,
    sum_run_cost,
)
from zeroth.runtime.orchestration.policy_gate import RuntimePolicyGate
from zeroth.runtime.orchestration.tool_executor import node_by_id

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
            failed_run = await self.policy_gate.enforce_loop_guards(graph, run, started_at)
            if failed_run is not None:
                return failed_run
            if not run.pending_node_ids:
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
                    # Still waiting (nested approval in the resumed branch).
                    run.pending_node_ids.insert(0, node_id)
                    return run
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
                            graph_ref, version
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
                    next_node_ids = self.plan_next_nodes(graph, run, node_id, output_data)
                    self.queue_next_nodes(graph, run, node_id, output_data, next_node_ids)
                    run.metadata["last_output"] = output_data
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
                next_node_ids = self.plan_next_nodes(graph, run, node_id, output_data)
                self.queue_next_nodes(graph, run, node_id, output_data, next_node_ids)
                run.metadata["last_output"] = output_data
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
            next_node_ids = self.plan_next_nodes(graph, run, node_id, output_data)
            self.queue_next_nodes(graph, run, node_id, output_data, next_node_ids)
            run.metadata["last_output"] = output_data
            run.status = RunStatus.RUNNING
            run.current_node_ids = []
            run.touch()
            run = await self.run_repository.put(run)
            await self.run_repository.write_checkpoint(run)
            await self.refresh_artifact_ttls(run)

    def plan_next_nodes(
        self,
        graph: Graph,
        run: Run,
        node_id: str,
        output_data: Mapping[str, Any],
    ) -> list[str]:
        """Decide which nodes to run next based on the current node's output.

        Uses the branch planner to evaluate edge conditions and figure out
        which outgoing edges are active. Updates the run's condition results
        and edge visit counts.
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
        return list(plan.next_node_ids)

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
            edge = self.edge_for(graph, source_node_id, target_node_id)
            payload = dict(output_data)
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
                payload = self.mapping_executor.execute(
                    output_data, edge.mapping, context=context_ns
                )
            payloads[target_node_id] = payload
            run.pending_node_ids.append(target_node_id)
        run.metadata["node_payloads"] = payloads

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
        """Mark a run as failed with the given reason and save it."""
        run.status = RunStatus.FAILED
        run.failure_state = RunFailureState(reason=reason, message=message)
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
