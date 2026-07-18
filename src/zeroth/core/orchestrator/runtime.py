"""Graph runtime orchestrator.

This module contains the main engine that executes a graph of agent nodes.
It walks through the graph step by step, running each node, checking policies,
handling human approvals, recording audit trails, and persisting run state
so that executions can be resumed if interrupted.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from zeroth.core.agent_runtime import AgentRunner, RepositoryThreadResolver
from zeroth.core.agent_runtime.errors import BudgetExceededError
from zeroth.core.approvals import ApprovalRecord, ApprovalService
from zeroth.core.audit import AuditRepository
from zeroth.core.audit.models import MemoryAccessRecord, ToolCallRecord
from zeroth.core.conditions import NextStepPlanner
from zeroth.core.conditions.models import ConditionContext, TraversalState
from zeroth.core.execution_units import ExecutableUnitRunner
from zeroth.core.graph import (
    AgentNode,
    Graph,
    HumanApprovalNode,
    Node,
    RetrievalNode,
    SubgraphNode,
)
from zeroth.core.mappings import MappingExecutor
from zeroth.core.observability import start_span
from zeroth.core.parallel.errors import (
    FanOutValidationError,
    ParallelExecutionError,
)
from zeroth.core.parallel.executor import ParallelExecutor
from zeroth.core.parallel.models import (
    BranchContext,
    FanInResult,
    GlobalStepTracker,
)
from zeroth.core.policy import Capability, PolicyGuard
from zeroth.core.runs import Run, RunFailureState, RunRepository, RunStatus
from zeroth.core.secrets import SecretResolver
from zeroth.core.subgraph.errors import (
    SubgraphCycleError,
    SubgraphDepthLimitError,
    SubgraphExecutionError,
    SubgraphResolutionError,
)
from zeroth.core.subgraph.resolver import merge_governance, namespace_subgraph
from zeroth.runtime.orchestration.audit_recorder import RuntimeAuditRecorder
from zeroth.runtime.orchestration.dispatcher import NodeDispatcher
from zeroth.runtime.orchestration.errors import (
    MemoryBindingResolutionError as MemoryBindingResolutionError,
)
from zeroth.runtime.orchestration.errors import (
    NodeDispatcherError as NodeDispatcherError,
)
from zeroth.runtime.orchestration.errors import (
    OrchestratorError as OrchestratorError,
)
from zeroth.runtime.orchestration.parallel_executor import RuntimeParallelExecutor
from zeroth.runtime.orchestration.parallel_executor import sum_run_cost as _sum_run_cost
from zeroth.runtime.orchestration.policy_gate import RuntimePolicyGate
from zeroth.runtime.orchestration.tool_executor import RuntimeToolExecutor

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RuntimeOrchestrator:
    """The main engine that runs a graph of agents from start to finish.

    Give it a graph and some input, and it will walk through each node
    in order, run the appropriate agent or executable unit, handle
    branching logic, enforce policies, manage human approval gates,
    and keep a full audit trail. Run state is saved after every step
    so execution can be resumed if interrupted.
    """

    run_repository: RunRepository
    agent_runners: Mapping[str, AgentRunner]
    executable_unit_runner: ExecutableUnitRunner
    audit_repository: AuditRepository | None = None
    policy_guard: PolicyGuard | None = None
    approval_service: ApprovalService | None = None
    secret_resolver: SecretResolver | None = None
    thread_resolver: RepositoryThreadResolver | None = None
    webhook_service: object | None = None  # Optional WebhookService for event emission
    # Phase 18: Cost instrumentation for provider adapter wrapping.
    regulus_client: object | None = None
    cost_estimator: object | None = None
    deployment_ref: str | None = None
    # Phase 20: Memory and budget injection for AgentRunner dispatch.
    memory_resolver: object | None = None
    budget_enforcer: object | None = None
    # Optional per-run cumulative cost ceiling (USD). Enforced locally from the
    # run's own audit cost_usd — independent of budget_enforcer/regulus, so it
    # works with the control plane disabled. Post-hoc: the run halts on the NEXT
    # node once cumulative spend crosses the cap. ``None`` disables it.
    per_run_cap_usd: float | None = None
    # Phase 34: Artifact store for large payload externalization.
    artifact_store: Any | None = None
    # Phase 35: Resilient HTTP client for managed external calls.
    http_client: Any | None = None
    # Phase 36: Template registry and renderer for prompt template resolution.
    template_registry: Any | None = None
    template_renderer: Any | None = None
    # Phase 37: Context window management flag (enables tracker injection).
    context_window_enabled: bool = True
    # Phase 38: Parallel fan-out/fan-in executor.
    parallel_executor: ParallelExecutor = ParallelExecutor()
    branch_planner: NextStepPlanner = NextStepPlanner()
    mapping_executor: MappingExecutor = MappingExecutor()
    # Phase 39: Subgraph composition executor (typed as Any to avoid circular import).
    subgraph_executor: Any | None = None

    async def run_graph(
        self,
        graph: Graph,
        initial_input: Mapping[str, Any],
        *,
        deployment_ref: str | None = None,
        thread_id: str | None = None,
    ) -> Run:
        """Start a fresh execution of a graph with the given input.

        Creates a new Run, persists it, and drives the graph to completion
        (or until it hits an approval gate or failure). Returns the final
        Run object with status, outputs, and history.
        """
        run = Run(
            graph_version_ref=self._graph_version_ref(graph),
            deployment_ref=deployment_ref or graph.graph_id,
            thread_id=thread_id or "",
            current_node_ids=[],
            pending_node_ids=[self._entry_step(graph)],
            metadata=self._initial_metadata(graph, initial_input),
        )
        persisted = await self.run_repository.create(run)
        persisted.status = RunStatus.RUNNING
        persisted.touch()
        persisted = await self.run_repository.put(persisted)
        await self.run_repository.write_checkpoint(persisted)
        with start_span(
            "zeroth.run",
            {
                "zeroth.run_id": persisted.run_id,
                "zeroth.graph_version": persisted.graph_version_ref,
                "zeroth.deployment_ref": persisted.deployment_ref,
            },
        ):
            return await self._drive(graph, persisted)

    async def resume_graph(self, graph: Graph, run_id: str) -> Run:
        """Resume an existing run that was paused or waiting for approval.

        Looks up the run by ID and continues driving the graph from where
        it left off. Raises KeyError if the run doesn't exist, or
        OrchestratorError if the run can't be resumed (e.g., already completed).
        """
        run = await self.run_repository.get(run_id)
        if run is None:
            raise KeyError(run_id)
        if run.status not in {RunStatus.RUNNING, RunStatus.PENDING, RunStatus.WAITING_APPROVAL}:
            raise OrchestratorError(f"run {run_id} is not resumable from status {run.status}")
        with start_span(
            "zeroth.run",
            {
                "zeroth.run_id": run.run_id,
                "zeroth.graph_version": run.graph_version_ref,
                "zeroth.deployment_ref": run.deployment_ref,
            },
        ):
            return await self._drive(graph, run)

    async def _refresh_artifact_ttls(self, run: Run) -> None:
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
            from zeroth.core.artifacts.helpers import refresh_artifact_ttls

            combined: dict[str, Any] = {}
            for i, entry in enumerate(run.execution_history):
                combined[f"_history_{i}"] = entry.output_snapshot
            if run.final_output is not None:
                combined["_final_output"] = run.final_output
            await refresh_artifact_ttls(self.artifact_store, combined, ttl=3600)
        except Exception:
            logger.exception("artifact TTL refresh failed (non-fatal)")

    async def _drive(
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
            failed_run = await self._enforce_loop_guards(graph, run, started_at)
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
                await self._refresh_artifact_ttls(persisted)
                await self._emit_webhook(
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
            node = self._node_by_id(graph, node_id)
            # Each node consumes the payload that was prepared for it by the previous step.
            input_payload = self._payload_for(run, node_id)
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
                fan_in_resume = await self._execute_parallel_fan_out_resume(
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
                self._merge_fan_in_state(run, fan_in_resume)
                merged_output = fan_in_resume.merged_output
                downstream_ids = self._plan_next_nodes(graph, run, node_id, merged_output)
                for ds_id in downstream_ids:
                    self._increment_node_visit(run, ds_id)
                    post_fan_in_ids = self._plan_next_nodes(graph, run, ds_id, merged_output)
                    self._queue_next_nodes(graph, run, ds_id, merged_output, post_fan_in_ids)
                run.metadata["last_output"] = merged_output
                run.touch()
                run = await self.run_repository.put(run)
                await self.run_repository.write_checkpoint(run)
                continue

            pending_approval = await self._consume_side_effect_approval(run, node, input_payload)
            if pending_approval is not None:
                return pending_approval

            denial = await self._enforce_policy(graph, run, node, input_payload)
            if denial is not None:
                return denial

            side_effect_gate = await self._gate_policy_required_side_effects(
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
                    await self._emit_webhook(
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
                await self._refresh_artifact_ttls(persisted)
                return persisted

            # Phase 39: Subgraph composition -- delegate to SubgraphExecutor.
            if isinstance(node, SubgraphNode):
                if self.subgraph_executor is None:
                    return await self._fail_run(
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
                        return await self._fail_run(run, "subgraph_resume_failed", str(exc))

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
                        await self._refresh_artifact_ttls(persisted)
                        return persisted

                    if child_run.status != RunStatus.COMPLETED:
                        failure = child_run.failure_state
                        detail = failure.message if failure is not None else "unknown failure"
                        return await self._fail_run(
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
                    await self._record_history(
                        run,
                        node,
                        node_id,
                        input_payload,
                        output_data,
                        audit_record,
                        started_at=node_started_at,
                    )
                    self._increment_node_visit(run, node_id)
                    next_node_ids = self._plan_next_nodes(graph, run, node_id, output_data)
                    self._queue_next_nodes(graph, run, node_id, output_data, next_node_ids)
                    run.metadata["last_output"] = output_data
                    run.touch()
                    persisted = await self.run_repository.put(run)
                    await self.run_repository.write_checkpoint(persisted)
                    await self._refresh_artifact_ttls(persisted)
                    continue

                # Path A: First encounter -- no pending_subgraph for this node.
                try:
                    with start_span(
                        "zeroth.subgraph",
                        {"zeroth.node_id": node_id, "zeroth.run_id": run.run_id},
                    ):
                        child_run = await self.subgraph_executor.execute(
                            orchestrator=self,
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
                    return await self._fail_run(run, "subgraph_execution_failed", str(exc))

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
                    await self._refresh_artifact_ttls(persisted)
                    return persisted

                if child_run.status != RunStatus.COMPLETED:
                    failure = child_run.failure_state
                    detail = failure.message if failure is not None else "unknown failure"
                    return await self._fail_run(
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
                await self._record_history(
                    run,
                    node,
                    node_id,
                    input_payload,
                    output_data,
                    audit_record,
                    started_at=node_started_at,
                )
                self._increment_node_visit(run, node_id)
                next_node_ids = self._plan_next_nodes(graph, run, node_id, output_data)
                self._queue_next_nodes(graph, run, node_id, output_data, next_node_ids)
                run.metadata["last_output"] = output_data
                run.touch()
                persisted = await self.run_repository.put(run)
                await self.run_repository.write_checkpoint(persisted)
                await self._refresh_artifact_ttls(persisted)
                continue

            try:
                # Per-run cost ceiling (local, control-plane-independent). Post-hoc:
                # cumulative spend from prior nodes is read from the run's own audit
                # cost_usd, so the run halts on the NEXT node once it crosses the cap.
                if self.per_run_cap_usd is not None:
                    spent = _sum_run_cost(run)
                    if spent >= self.per_run_cap_usd:
                        raise BudgetExceededError(
                            f"per-run budget exceeded: ${spent:.4f} >= ${self.per_run_cap_usd:.4f}",
                            spend=spent,
                            cap=self.per_run_cap_usd,
                        )
                output_data, audit_record = await self._dispatch_node(
                    node, run, input_payload, graph
                )
            except Exception as exc:
                await self._record_failed_execution_audit(
                    run, node, node_id, input_payload, exc, started_at=node_started_at
                )
                return await self._fail_run(run, "node_execution_failed", str(exc))

            # Phase 38: Parallel fan-out detection.
            parallel_config = getattr(node, "parallel_config", None)
            if parallel_config is not None:
                try:
                    with start_span(
                        "zeroth.fanout",
                        {"zeroth.node_id": node_id, "zeroth.run_id": run.run_id},
                    ):
                        fan_in_result = await self._execute_parallel_fan_out(
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
                    return await self._fail_run(run, "parallel_execution_failed", str(exc))
                # D-11: Check for run-wide approval pause from a branch's subgraph.
                if fan_in_result.pause_state is not None:
                    return await self._handle_parallel_subgraph_pause(
                        run,
                        node,
                        node_id,
                        input_payload,
                        output_data,
                        fan_in_result,
                    )
                # Record the source node's own history (the node that triggered fan-out)
                await self._record_history(
                    run,
                    node,
                    node_id,
                    input_payload,
                    output_data,
                    audit_record,
                    started_at=node_started_at,
                )
                self._increment_node_visit(run, node_id)
                # Merge branch histories and audit refs into parent run
                self._merge_fan_in_state(run, fan_in_result)
                # Use merged output for downstream planning.
                # The downstream nodes (one hop from source) were already executed
                # inside branches. Plan next from those downstream nodes instead.
                merged_output = fan_in_result.merged_output
                downstream_ids = self._plan_next_nodes(graph, run, node_id, output_data)
                for ds_id in downstream_ids:
                    self._increment_node_visit(run, ds_id)
                    post_fan_in_ids = self._plan_next_nodes(graph, run, ds_id, merged_output)
                    self._queue_next_nodes(graph, run, ds_id, merged_output, post_fan_in_ids)
                run.metadata["last_output"] = merged_output
                run.status = RunStatus.RUNNING
                run.current_node_ids = []
                run.touch()
                run = await self.run_repository.put(run)
                await self.run_repository.write_checkpoint(run)
                await self._refresh_artifact_ttls(run)
                continue

            await self._record_history(
                run,
                node,
                node_id,
                input_payload,
                output_data,
                audit_record,
                started_at=node_started_at,
            )
            self._increment_node_visit(run, node_id)
            next_node_ids = self._plan_next_nodes(graph, run, node_id, output_data)
            self._queue_next_nodes(graph, run, node_id, output_data, next_node_ids)
            run.metadata["last_output"] = output_data
            run.status = RunStatus.RUNNING
            run.current_node_ids = []
            run.touch()
            run = await self.run_repository.put(run)
            await self.run_repository.write_checkpoint(run)
            await self._refresh_artifact_ttls(run)

    @property
    def _parallel_runtime(self) -> RuntimeParallelExecutor:
        """The fan-out collaborator, built from this orchestrator's own dependencies.

        ``orchestrator=self`` is handed over explicitly because
        ``SubgraphExecutor.execute`` takes the orchestrator by keyword as part
        of its published contract; a branch running a subgraph has no other way
        to satisfy it.
        """
        return RuntimeParallelExecutor(
            run_repository=self.run_repository,
            refresh_artifact_ttls=self._refresh_artifact_ttls,
            parallel_executor=self.parallel_executor,
            audit_recorder=self._audit_recorder,
            node_dispatcher=self._node_dispatcher,
            policy_gate=self._policy_gate,
            subgraph_executor=self.subgraph_executor,
            budget_enforcer=self.budget_enforcer,
            orchestrator=self,
            plan_next_nodes=self._plan_next_nodes,
            resume_graph=self.resume_graph,
        )

    async def _execute_parallel_fan_out(
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
        """Execute parallel fan-out for a node with parallel_config."""
        return await self._parallel_runtime.execute_fan_out(
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

    async def _handle_parallel_subgraph_pause(
        self,
        run: Run,
        node: Node,
        node_id: str,
        input_payload: Mapping[str, Any],
        output_data: dict[str, Any],
        fan_in_result: FanInResult,
    ) -> Run:
        """Stash pending_parallel_subgraph and return run in WAITING_APPROVAL."""
        return await self._parallel_runtime.handle_subgraph_pause(
            run, node, node_id, input_payload, output_data, fan_in_result
        )

    async def _execute_parallel_fan_out_resume(
        self,
        graph: Graph,
        run: Run,
        node: Node,
        node_id: str,
        pending: dict[str, Any],
        *,
        step_tracker: GlobalStepTracker | None,
    ) -> FanInResult:
        """D-11 literal resume: reuse completed, resume paused, None-out cancelled."""
        return await self._parallel_runtime.execute_fan_out_resume(
            graph, run, node, node_id, pending, step_tracker=step_tracker
        )

    async def _enforce_policy_for_branch(
        self,
        graph: Graph,
        run: Run,
        node: Node,
        input_payload: Mapping[str, Any],
    ) -> str | None:
        """Check policy for a branch node dispatch. Returns denial reason or None."""
        return await self._policy_gate.enforce_policy_for_branch(graph, run, node, input_payload)

    def _merge_fan_in_state(self, run: Run, fan_in_result: FanInResult) -> None:
        """Merge branch execution state back into the parent Run."""
        self._parallel_runtime.merge_fan_in_state(run, fan_in_result)

    @property
    def _tool_executor(self) -> RuntimeToolExecutor:
        """The governed unit-invocation collaborator."""
        return RuntimeToolExecutor(executable_unit_runner=self.executable_unit_runner)

    @property
    def _node_dispatcher(self) -> NodeDispatcher:
        """The dispatch collaborator, built from this orchestrator's own dependencies."""
        return NodeDispatcher(
            agent_runners=self.agent_runners,
            executable_unit_runner=self.executable_unit_runner,
            tool_executor=self._tool_executor,
            policy_gate=self._policy_gate,
            thread_resolver=self.thread_resolver,
            secret_resolver=self.secret_resolver,
            memory_resolver=self.memory_resolver,
            budget_enforcer=self.budget_enforcer,
            regulus_client=self.regulus_client,
            cost_estimator=self.cost_estimator,
            deployment_ref=self.deployment_ref,
            template_registry=self.template_registry,
            template_renderer=self.template_renderer,
            context_window_enabled=self.context_window_enabled,
        )

    async def _dispatch_node(
        self,
        node: Node,
        run: Run,
        input_payload: Mapping[str, Any],
        graph: Graph | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Dispatch a node inside an OBS tracing span."""
        return await self._node_dispatcher.dispatch(node, run, input_payload, graph)

    async def _dispatch_node_inner(
        self,
        node: Node,
        run: Run,
        input_payload: Mapping[str, Any],
        graph: Graph | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Run a single node and return its output and audit data."""
        return await self._node_dispatcher.dispatch_inner(node, run, input_payload, graph)

    async def _dispatch_retrieval_node(
        self,
        node: RetrievalNode,
        run: Run,
        input_payload: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Retrieve grounded context from a vector connector for a RetrievalNode (RAG-01)."""
        return await self._node_dispatcher.dispatch_retrieval(node, run, input_payload)

    async def _resolve_thread(self, node: AgentNode, run: Run) -> str | None:
        """Figure out which thread ID an agent node should use."""
        return await self._node_dispatcher.resolve_thread(node, run)

    async def _resolve_template_memory(
        self,
        node: AgentNode,
        run: Run,
        thread_id: str | None,
        input_payload: Mapping[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Fetch memory values declared in ``template_memory_bindings``."""
        return await self._node_dispatcher.resolve_template_memory(
            node, run, thread_id, input_payload
        )

    def _plan_next_nodes(
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

    def _queue_next_nodes(
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
            edge = self._edge_for(graph, source_node_id, target_node_id)
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

    @property
    def _audit_recorder(self) -> RuntimeAuditRecorder:
        """The audit collaborator, built from this orchestrator's own dependencies.

        Rebuilt per access rather than cached: ``RuntimeOrchestrator`` is a
        slotted dataclass whose ``__init__`` signature is a pinned public
        contract, so the recorder cannot be stored as a field or an attribute.
        It is a frozen two-field dataclass, so construction is free.
        """
        return RuntimeAuditRecorder(
            audit_repository=self.audit_repository,
            secret_resolver=self.secret_resolver,
        )

    async def _record_history(
        self,
        run: Run,
        node: Node,
        node_id: str,
        input_payload: Mapping[str, Any],
        output_payload: Mapping[str, Any],
        audit_record: Mapping[str, Any],
        *,
        started_at: datetime | None = None,
    ) -> None:
        """Save a record of this node's execution to the run history and audit log."""
        await self._audit_recorder.record_history(
            run,
            node,
            node_id,
            input_payload,
            output_payload,
            audit_record,
            started_at=started_at,
        )

    def _increment_node_visit(self, run: Run, node_id: str) -> None:
        """Bump the visit counter for this node by one."""
        run.node_visit_counts[node_id] = run.node_visit_counts.get(node_id, 0) + 1

    async def _record_failed_execution_audit(
        self,
        run: Run,
        node: Node,
        node_id: str,
        input_payload: Mapping[str, Any],
        error: Exception,
        *,
        started_at: datetime | None = None,
    ) -> None:
        """Persist an audit record for execution failures that happen before completion."""
        await self._audit_recorder.record_failed_execution(
            run, node, node_id, input_payload, error, started_at=started_at
        )

    async def _record_failed_branch_execution_audit(
        self,
        run: Run,
        node: Node,
        node_id: str,
        input_payload: Mapping[str, Any],
        error: Exception,
        ctx: BranchContext,
    ) -> None:
        """Persist a branch-scoped audit record for a failed branch-node dispatch."""
        await self._audit_recorder.record_failed_branch_execution(
            run, node, node_id, input_payload, error, ctx
        )

    def _payload_for(self, run: Run, node_id: str) -> dict[str, Any]:
        """Get and remove the queued input payload for a node.

        Returns an empty dict if no payload was queued for this node.
        """
        payloads = dict(run.metadata.get("node_payloads", {}))
        payload = payloads.pop(node_id, None)
        run.metadata["node_payloads"] = payloads
        if payload is None:
            return {}
        return dict(payload)

    @property
    def _policy_gate(self) -> RuntimePolicyGate:
        """The policy collaborator, built from this orchestrator's own dependencies.

        Rebuilt per access for the same reason as ``_audit_recorder``: the
        pinned ``__init__`` signature forbids storing it, and construction of a
        frozen dataclass is free. ``fail_run`` and ``refresh_artifact_ttls`` are
        passed as bound callbacks so the gate never sees the orchestrator.
        """
        return RuntimePolicyGate(
            run_repository=self.run_repository,
            audit_recorder=self._audit_recorder,
            fail_run=self._fail_run,
            refresh_artifact_ttls=self._refresh_artifact_ttls,
            policy_guard=self.policy_guard,
            approval_service=self.approval_service,
            executable_unit_runner=self.executable_unit_runner,
            agent_runners=self.agent_runners,
        )

    async def _enforce_loop_guards(
        self,
        graph: Graph,
        run: Run,
        started_at: float,
    ) -> Run | None:
        """Check if the run has exceeded its step or time limits."""
        return await self._policy_gate.enforce_loop_guards(graph, run, started_at)

    async def _enforce_policy(
        self,
        graph: Graph,
        run: Run,
        node: Node,
        input_payload: Mapping[str, Any],
    ) -> Run | None:
        """Check if the policy guard allows this node to run."""
        return await self._policy_gate.enforce_policy(graph, run, node, input_payload)

    def _enforcement_context_for(self, run: Run, node_id: str) -> dict[str, Any]:
        """Return the stored policy enforcement context for a node, if any."""
        return self._policy_gate.enforcement_context_for(run, node_id)

    def _effective_capabilities_for(self, run: Run, node_id: str) -> set[Capability] | None:
        """Return the node's granted capability set, or None when enforcement is off."""
        return self._policy_gate.effective_capabilities_for(run, node_id)

    async def _gate_policy_required_side_effects(
        self,
        run: Run,
        node: Node,
        input_payload: Mapping[str, Any],
    ) -> Run | None:
        """Pause execution when policy requires approval before side effects."""
        return await self._policy_gate.gate_policy_required_side_effects(run, node, input_payload)

    async def _consume_side_effect_approval(
        self,
        run: Run,
        node: Node,
        input_payload: Mapping[str, Any],
    ) -> Run | None:
        """Resolve pending side-effect approval state before re-executing a node."""
        return await self._policy_gate.consume_side_effect_approval(run, node, input_payload)

    def _node_has_side_effects(self, node: Node) -> bool:
        """Detect whether a node can cause side effects that require approval."""
        return self._policy_gate.node_has_side_effects(node)

    async def _run_agent_with_optional_enforcement(
        self,
        runner: AgentRunner,
        input_payload: Mapping[str, Any],
        *,
        thread_id: str | None,
        runtime_context: Mapping[str, Any],
        enforcement_context: Mapping[str, Any],
    ) -> Any:
        """Call agent runners with enforcement context when their signature supports it."""
        return await self._node_dispatcher.run_agent_with_optional_enforcement(
            runner,
            input_payload,
            thread_id=thread_id,
            runtime_context=runtime_context,
            enforcement_context=enforcement_context,
        )

    async def _run_executable_unit_with_optional_enforcement(
        self,
        manifest_ref: str,
        input_payload: Mapping[str, Any],
        *,
        enforcement_context: Mapping[str, Any],
    ) -> Any:
        """Call executable-unit runners with enforcement context when supported."""
        return await self._tool_executor.run_unit(
            manifest_ref,
            input_payload,
            enforcement_context=enforcement_context,
        )

    def _tool_executor_for(
        self,
        graph: Graph,
        enforcement_context: Mapping[str, Any] | None = None,
    ) -> Any:
        """Build the executor that runs an agent's attached tool nodes."""
        return self._tool_executor.build(graph, enforcement_context)

    def _redact_for_audit(self, value: Any) -> Any:
        """Redact any resolved secret values before persisting audit material."""
        return self._audit_recorder.redact(value)

    @staticmethod
    def _typed_audit_fields(
        record: Mapping[str, Any],
    ) -> tuple[list[ToolCallRecord], list[MemoryAccessRecord]]:
        """Promote a runner audit record's tool calls / memory interactions to typed fields."""
        return RuntimeAuditRecorder.typed_fields(record)

    async def record_approval_resolution(
        self,
        *,
        graph: Graph,
        run: Run,
        node: HumanApprovalNode,
        output_payload: Mapping[str, Any],
        approval_record: ApprovalRecord,
    ) -> Run:
        """Record the result of a human approval decision and continue the run.

        Called after a human approves or rejects an approval gate. Updates
        the run history, plans the next nodes, and sets the run back to
        RUNNING status so it can be resumed.
        """
        if run.pending_node_ids and run.pending_node_ids[0] == node.node_id:
            run.pending_node_ids.pop(0)
        action = (
            approval_record.resolution.decision.value if approval_record.resolution else "approve"
        )
        # Record approval outcomes like normal node completions for downstream flow.
        audit_record = {
            "approval_id": approval_record.approval_id,
            "decision": action,
            "actor": (
                approval_record.resolution.actor.model_dump(mode="json")
                if approval_record.resolution
                else None
            ),
        }
        await self._record_history(
            run,
            node,
            node.node_id,
            approval_record.proposed_payload or {},
            output_payload,
            audit_record,
        )
        self._increment_node_visit(run, node.node_id)
        next_node_ids = self._plan_next_nodes(graph, run, node.node_id, output_payload)
        self._queue_next_nodes(graph, run, node.node_id, output_payload, next_node_ids)
        run.metadata["last_output"] = dict(output_payload)
        run.current_node_ids = []
        run.pending_approval = None
        run.status = RunStatus.RUNNING
        run.touch()
        run = await self.run_repository.put(run)
        await self.run_repository.write_checkpoint(run)
        await self._refresh_artifact_ttls(run)
        return run

    async def _fail_run(self, run: Run, reason: str, message: str) -> Run:
        """Mark a run as failed with the given reason and save it."""
        run.status = RunStatus.FAILED
        run.failure_state = RunFailureState(reason=reason, message=message)
        run.metadata["termination_reason"] = reason
        run.touch()
        persisted = await self.run_repository.put(run)
        await self.run_repository.write_checkpoint(persisted)
        await self._refresh_artifact_ttls(persisted)
        await self._emit_webhook(
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

    async def _emit_webhook(
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

    def _entry_step(self, graph: Graph) -> str:
        """Get the ID of the first node to run in the graph."""
        if graph.entry_step is not None:
            return graph.entry_step
        if not graph.nodes:
            raise OrchestratorError("graph has no nodes")
        return graph.nodes[0].node_id

    def _graph_version_ref(self, graph: Graph) -> str:
        """Build a version reference string like 'my-graph:v2'."""
        return f"{graph.graph_id}:v{graph.version}"

    def _stored_audit_id(self, run_id: str, audit_ref: str) -> str:
        """Namespace persisted audit IDs by run so append-only storage stays globally unique."""
        return RuntimeAuditRecorder.stored_audit_id(run_id, audit_ref)

    def _initial_metadata(self, graph: Graph, initial_input: Mapping[str, Any]) -> dict[str, Any]:
        """Build the starting metadata dict for a new run."""
        return {
            "graph_id": graph.graph_id,
            "graph_name": graph.name,
            "node_payloads": {self._entry_step(graph): dict(initial_input)},
            "edge_visit_counts": {},
            "path": [],
            "audits": {},
        }

    def _node_by_id(self, graph: Graph, node_id: str) -> Node:
        """Find a node in the graph by its ID. Raises KeyError if not found."""
        for node in graph.nodes:
            if node.node_id == node_id:
                return node
        raise KeyError(node_id)

    def _edge_for(self, graph: Graph, source_node_id: str, target_node_id: str):
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
