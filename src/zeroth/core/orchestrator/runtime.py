"""Graph runtime orchestrator.

This module contains the main engine that executes a graph of agent nodes.
It walks through the graph step by step, running each node, checking policies,
handling human approvals, recording audit trails, and persisting run state
so that executions can be resumed if interrupted.
"""

from __future__ import annotations

import contextlib
import inspect
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from zeroth.core.agent_runtime import AgentRunner, RepositoryThreadResolver
from zeroth.core.approvals import ApprovalDecision, ApprovalRecord, ApprovalService
from zeroth.core.audit import AuditRepository, NodeAuditRecord
from zeroth.core.audit.models import TokenUsage
from zeroth.core.conditions import NextStepPlanner
from zeroth.core.conditions.models import ConditionContext, TraversalState
from zeroth.core.execution_units import ExecutableUnitRunner
from zeroth.core.graph import (
    AgentNode,
    ExecutableUnitNode,
    Graph,
    HumanApprovalNode,
    HumanApprovalNodeData,
    Node,
    SubgraphNode,
)
from zeroth.core.mappings import MappingExecutor
from zeroth.core.parallel.errors import (
    BranchApprovalPauseSignal,
    FanOutValidationError,
    ParallelExecutionError,
)
from zeroth.core.parallel.executor import ParallelExecutor
from zeroth.core.parallel.models import (
    BranchContext,
    BranchResult,
    FanInResult,
    GlobalStepTracker,
)
from zeroth.core.policy import PolicyDecision, PolicyGuard
from zeroth.core.runs import Run, RunFailureState, RunHistoryEntry, RunRepository, RunStatus
from zeroth.core.secrets import SecretResolver
from zeroth.core.subgraph.errors import (
    SubgraphCycleError,
    SubgraphDepthLimitError,
    SubgraphExecutionError,
    SubgraphResolutionError,
)
from zeroth.core.subgraph.resolver import merge_governance, namespace_subgraph

logger = logging.getLogger(__name__)

# Sentinel for "attribute not present" in optional runner wiring.
_MISSING: Any = object()

# Maps the string scope names used in TemplateMemoryBinding to MemoryScope enum values.
# Imported lazily inside _resolve_template_memory to avoid a hard top-level dependency.
_SCOPE_NAMES: frozenset[str] = frozenset({"run", "thread", "shared"})

# Regex for {namespace.field} placeholders supported in binding key / key_prefix.
_KEY_PLACEHOLDER_RE = re.compile(r"\{(input|state|run)\.([^}]+)\}")


def _substitute_tmb_key(
    key: str,
    *,
    input_payload: dict[str, Any],
    state: dict[str, Any],
    run_id: str,
) -> str:
    """Replace ``{input.field}``, ``{state.field}``, ``{run.run_id}`` placeholders in a key.

    Unknown placeholders are left unchanged so callers can detect them.
    """
    sources: dict[str, dict[str, Any]] = {
        "input": input_payload,
        "state": state,
        "run": {"run_id": run_id},
    }

    def _replace(m: re.Match) -> str:  # type: ignore[type-arg]
        namespace, field = m.group(1), m.group(2)
        ns = sources.get(namespace, {})
        return str(ns[field]) if field in ns else m.group(0)

    return _KEY_PLACEHOLDER_RE.sub(_replace, key)


def _sum_run_cost(run: Run) -> float:
    """Return the child Run's aggregated cost_usd for BranchResult rollup.

    Reads the `total_cost_usd` key written by `SubgraphExecutor.execute`
    on the child run's metadata at return-time (W-4 cost-rollup
    location). Falls back to walking `execution_history` entries for
    any `cost_usd` field if the explicit aggregation key is absent.
    `_drive()` does NOT write this key — the only writer is
    `SubgraphExecutor.execute`.
    """
    explicit = run.metadata.get("total_cost_usd")
    if explicit is not None:
        with contextlib.suppress(TypeError, ValueError):
            return float(explicit)
    total = 0.0
    for entry in run.execution_history or []:
        cost: Any = None
        if isinstance(entry, dict):
            cost = entry.get("cost_usd")
        else:
            cost = getattr(entry, "cost_usd", None)
        if cost:
            with contextlib.suppress(TypeError, ValueError):
                total += float(cost)
    return total


class OrchestratorError(RuntimeError):
    """Something went wrong during graph orchestration.

    This is the base error for all orchestrator-related problems.
    Catch this if you want to handle any orchestration failure.
    """


class NodeDispatcherError(OrchestratorError):
    """A specific node could not be executed.

    Raised when the orchestrator doesn't know how to run a particular
    node type, or when no runner is registered for an agent node.
    """


class MemoryBindingResolutionError(OrchestratorError):
    """A template memory binding could not be resolved.

    Raised when a connector referenced in ``template_memory_bindings`` is
    not registered, or when fetching the value from the connector fails.
    """


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
                        return await self._fail_run(
                            run, "subgraph_resume_failed", str(exc)
                        )

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
                        run, node, node_id, input_payload, output_data, audit_record
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
                    run, node, node_id, input_payload, output_data, audit_record
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
                output_data, audit_record = await self._dispatch_node(node, run, input_payload)
            except Exception as exc:
                await self._record_failed_execution_audit(run, node, node_id, input_payload, exc)
                return await self._fail_run(run, "node_execution_failed", str(exc))

            # Phase 38: Parallel fan-out detection.
            parallel_config = getattr(node, "parallel_config", None)
            if parallel_config is not None:
                try:
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

            await self._record_history(run, node, node_id, input_payload, output_data, audit_record)
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
        """Execute parallel fan-out for a node with parallel_config.

        Splits the node's output into N branches, executes downstream nodes
        for each branch concurrently, and collects results into a FanInResult.
        Budget is checked before spawning. A GlobalStepTracker enforces the
        aggregate step limit across all branches.
        """
        from zeroth.core.parallel.models import ParallelConfig as _ParallelConfig

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
        downstream_node_ids = self._plan_next_nodes(graph, run, node_id, output_data)

        async def branch_coro_factory(ctx: BranchContext) -> dict[str, Any]:
            """Execute downstream nodes for a single branch."""
            branch_output: dict[str, Any] = dict(ctx.input_payload)

            for ds_node_id in downstream_node_ids:
                ds_node = self._node_by_id(graph, ds_node_id)

                # Per-branch policy enforcement
                policy_result = await self._enforce_policy_for_branch(
                    graph,
                    run,
                    ds_node,
                    branch_output,
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
                    child_run = await self.subgraph_executor.execute(
                        orchestrator=self,
                        parent_graph=graph,
                        parent_run=run,
                        node=ds_node,
                        node_id=ds_node_id,
                        input_payload=dict(branch_output),
                        branch_context=ctx,
                        step_tracker=step_tracker,
                    )
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
                    child_output = child_run.final_output or {}
                    if not isinstance(child_output, dict):
                        child_output = {"result": child_output}
                    ds_output = child_output
                    ds_audit = {
                        "subgraph_run_id": child_run.run_id,
                        "subgraph_graph_ref": ds_node.subgraph.graph_ref,
                        "subgraph_status": child_run.status.value,
                        "cost_usd": _sum_run_cost(child_run),
                    }
                else:
                    # Dispatch the downstream node with branch-isolated payload
                    ds_output, ds_audit = await self._dispatch_node(
                        ds_node, run, branch_output
                    )

                # Increment global step tracker
                await step_tracker.increment()

                # Add branch_id to audit metadata
                ds_audit_with_branch = dict(ds_audit)
                ds_audit_with_branch["branch_id"] = ctx.branch_id
                ds_audit_with_branch["branch_index"] = ctx.branch_index

                # Record to branch-isolated state
                audit_seq = len(ctx.audit_refs) + 1
                audit_ref = f"{run.run_id}:branch:{ctx.branch_index}:audit:{audit_seq}"
                ctx.audit_refs.append(audit_ref)

                # Write audit record if audit repo available
                if self.audit_repository is not None:
                    await self.audit_repository.write(
                        NodeAuditRecord(
                            audit_id=audit_ref,
                            run_id=run.run_id,
                            thread_id=run.thread_id,
                            node_id=ds_node_id,
                            node_version=ds_node.node_version,
                            graph_version_ref=run.graph_version_ref,
                            deployment_ref=run.deployment_ref,
                            attempt=1,
                            status="completed",
                            input_snapshot=dict(branch_output),
                            output_snapshot=dict(ds_output),
                            execution_metadata=ds_audit_with_branch,
                        )
                    )

                # Append to branch execution history
                ctx.execution_history.append(
                    RunHistoryEntry(
                        node_id=ds_node_id,
                        status="completed",
                        input_snapshot=dict(branch_output),
                        output_snapshot=dict(ds_output),
                        audit_ref=audit_ref,
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
            pause_state: dict[str, Any] = {
                "paused": {
                    "branch_index": pause.branch_index,
                    "child_run_id": pause.child_run_id,
                    "graph_ref": pause.graph_ref,
                    "version": pause.version,
                    "node_id": pause.node_id,
                    "branch_context": (
                        {
                            "branch_index": paused_ctx.branch_index,
                            "branch_id": paused_ctx.branch_id,
                            "input_payload": dict(paused_ctx.input_payload),
                        }
                        if paused_ctx is not None
                        else None
                    ),
                },
                "completed_branch_results": list(
                    getattr(pause, "completed_branch_results", []) or []
                ),
                "cancelled_branch_contexts": [
                    {
                        "branch_index": cctx.branch_index,
                        "branch_id": cctx.branch_id,
                        "input_payload": dict(cctx.input_payload),
                    }
                    for cctx in getattr(pause, "cancelled_branch_contexts", []) or []
                ],
                "split_input": dict(output_data),
            }
            return FanInResult(results=[], pause_state=pause_state)

        # Enrich results with branch state + per-branch cost rollup (D-09)
        for ctx, result in zip(branch_contexts, branch_results, strict=False):
            if result.error is None:
                result.audit_refs = list(ctx.audit_refs)
                result.execution_history = list(ctx.execution_history)
                # Cost rollup: the SubgraphNode branch path stashed per-step
                # cost in the audit metadata; sum the entries on ctx to get
                # the per-branch cost (read from ds_audit["cost_usd"] fields
                # that the factory wrote into the branch history).
                branch_cost = 0.0
                for entry in ctx.execution_history:
                    audit = getattr(entry, "execution_metadata", None)
                    if isinstance(audit, dict) and "cost_usd" in audit:
                        with contextlib.suppress(TypeError, ValueError):
                            branch_cost += float(audit["cost_usd"])
                result.cost_usd = branch_cost

        # Collect fan-in
        return self.parallel_executor.collect_fan_in(branch_results, config, output_data)

    async def _handle_parallel_subgraph_pause(
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
                "audit_refs": list(br.audit_refs),
                "execution_history": [
                    e.model_dump() if hasattr(e, "model_dump") else e
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
        }
        run.status = RunStatus.WAITING_APPROVAL
        run.pending_node_ids.insert(0, node_id)
        run.touch()
        persisted = await self.run_repository.put(run)
        await self.run_repository.write_checkpoint(persisted)
        await self._refresh_artifact_ttls(persisted)
        return persisted

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
        from zeroth.core.parallel.models import ParallelConfig as _ParallelConfig

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
                    cost_usd=float(d.get("cost_usd", 0.0)),
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
                orchestrator=self,
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
                paused_graph_ref, paused_version
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
                },
            )

        resumed_output = resumed_child_run.final_output or {}
        if not isinstance(resumed_output, dict):
            resumed_output = {"result": resumed_output}
        paused_result = BranchResult(
            branch_index=paused_branch_index,
            output=resumed_output,
            error=None,
            audit_refs=[],
            execution_history=[],
            cost_usd=_sum_run_cost(resumed_child_run),
        )

        # 3. Record cancelled siblings as None-output BranchResults (D-19).
        cancelled_results: list[BranchResult] = [
            BranchResult(
                branch_index=int(ctx.get("branch_index", -1)),
                output=None,
                error="cancelled_by_approval_pause",
                audit_refs=[],
                execution_history=[],
                cost_usd=0.0,
            )
            for ctx in pending.get("cancelled_branches", [])
        ]

        # 4. Merge into branch-index order and run through collect_fan_in.
        all_results = completed_results + [paused_result] + cancelled_results
        all_results.sort(key=lambda br: br.branch_index)
        return self.parallel_executor.collect_fan_in(
            all_results, config, pending.get("split_input", {})
        )

    async def _enforce_policy_for_branch(
        self,
        graph: Graph,
        run: Run,
        node: Node,
        input_payload: Mapping[str, Any],
    ) -> str | None:
        """Check policy for a branch node dispatch. Returns denial reason or None."""
        guard = self.policy_guard
        if guard is None:
            return None
        result = guard.evaluate(graph, node, run, input_payload)
        if result.decision is PolicyDecision.ALLOW:
            return None
        return result.reason or "policy denied execution"

    def _merge_fan_in_state(self, run: Run, fan_in_result: FanInResult) -> None:
        """Merge branch execution state back into the parent Run.

        Appends all branch execution_history entries and audit_refs to the
        parent run so that the full trace is visible in the run record.
        """
        for branch_result in fan_in_result.results:
            for entry in branch_result.execution_history:
                run.execution_history.append(entry)
            for ref in branch_result.audit_refs:
                run.audit_refs.append(ref)
        run.completed_steps = [entry.node_id for entry in run.execution_history]

    async def _dispatch_node(
        self,
        node: Node,
        run: Run,
        input_payload: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Run a single node and return its output and audit data.

        Figures out what kind of node it is (agent or executable unit),
        finds the right runner, and executes it. Raises NodeDispatcherError
        if the node type isn't supported or no runner is registered.
        """
        if isinstance(node, AgentNode):
            runner = self.agent_runners.get(node.node_id)
            if runner is None:
                raise NodeDispatcherError(f"no agent runner registered for {node.node_id}")

            # Resolve thread before template rendering so memory can use thread scope.
            thread_id = await self._resolve_thread(node, run)
            tmb_audit_records: list[dict[str, Any]] = []

            # Phase 36: Template resolution -- resolve and render before agent execution.
            effective_instruction: str | None = None
            rendered_prompt_for_audit: str | None = None
            template_ref_for_audit: dict[str, Any] | None = None
            agent_template_ref = getattr(node.agent, "template_ref", None)
            if (
                self.template_registry is not None
                and self.template_renderer is not None
                and agent_template_ref is not None
            ):
                from zeroth.core.templates import TemplateRegistry, TemplateRenderer

                template_ref = node.agent.template_ref
                registry: TemplateRegistry = self.template_registry
                renderer: TemplateRenderer = self.template_renderer
                template = registry.get(template_ref.name, template_ref.version)
                # Resolve template memory bindings before building render_vars.
                _memory_ns, _tmb_records = await self._resolve_template_memory(
                    node, run, thread_id, input_payload
                )
                tmb_audit_records.extend(_tmb_records)
                render_vars: dict[str, Any] = {
                    "input": dict(input_payload),
                    "state": dict(run.metadata) if run.metadata else {},
                    "memory": _memory_ns,
                }
                render_result = renderer.render(template, render_vars)
                effective_instruction = render_result.rendered
                rendered_prompt_for_audit = render_result.rendered

                # Phase 36: Redact secret variable values before audit storage.
                from zeroth.core.templates.redaction import (
                    identify_secret_variables,
                    redact_rendered_prompt,
                )

                # Flatten nested render_vars for redaction matching.
                render_vars_flat: dict[str, object] = {}
                for _ns, _vals in render_vars.items():
                    if isinstance(_vals, dict):
                        for k, v in _vals.items():
                            render_vars_flat[k] = v
                secret_vars = identify_secret_variables(
                    list(render_vars_flat.keys()),
                )
                if secret_vars:
                    rendered_prompt_for_audit = redact_rendered_prompt(
                        render_result.rendered,
                        render_vars_flat,
                        secret_vars,
                    )

                template_ref_for_audit = {
                    "name": template.name,
                    "version": template.version,
                }

            # Phase 36: Override runner config instruction with rendered template.
            original_config = getattr(runner, "config", _MISSING)
            if effective_instruction is not None and original_config is not _MISSING:
                runner.config = original_config.model_copy(
                    update={"instruction": effective_instruction}
                )

            # Phase 18: Wrap provider with cost instrumentation (per ECON-01).
            # Use getattr so lightweight test runners without a .provider
            # attribute (e.g. FunctionalRunner, RecordingAgentRunner) still work.
            original_provider = getattr(runner, "provider", _MISSING)
            if (
                original_provider is not _MISSING
                and self.regulus_client is not None
                and self.cost_estimator is not None
            ):
                try:
                    from zeroth.core.econ.adapter import InstrumentedProviderAdapter

                    tenant_id = (
                        run.metadata.get("tenant_id", "default") if run.metadata else "default"
                    )
                    runner.provider = InstrumentedProviderAdapter(
                        inner=original_provider,
                        regulus_client=self.regulus_client,
                        cost_estimator=self.cost_estimator,
                        node_id=node.node_id,
                        run_id=run.run_id,
                        tenant_id=tenant_id,
                        deployment_ref=self.deployment_ref or "unknown",
                    )
                except ImportError:
                    pass

            # Phase 20: Save originals before injection so we can restore in finally.
            # getattr-based so mock runners without these attributes don't crash.
            # Injection is additive: only fill in when the runner has no resolver
            # of its own, so callers that pre-configure a runner with a specific
            # registry (e.g. tests) are respected.
            original_memory_resolver = getattr(runner, "memory_resolver", _MISSING)
            original_budget_enforcer = getattr(runner, "budget_enforcer", _MISSING)
            if (
                self.memory_resolver is not None
                and original_memory_resolver is not _MISSING
                and original_memory_resolver is None
            ):
                runner.memory_resolver = self.memory_resolver
            if (
                self.budget_enforcer is not None
                and original_budget_enforcer is not _MISSING
                and original_budget_enforcer is None
            ):
                runner.budget_enforcer = self.budget_enforcer

            # Phase 37: Context window tracker injection (per D-09, D-11).
            original_context_tracker = getattr(runner, "context_tracker", _MISSING)
            if (
                self.context_window_enabled
                and original_context_tracker is not _MISSING
                and original_context_tracker is None
                and hasattr(node.agent, "context_window")
                and node.agent.context_window is not None
            ):
                from zeroth.core.context_window import (
                    ContextWindowTracker,
                    LLMSummarizationStrategy,
                    ObservationMaskingStrategy,
                    TruncationStrategy,
                )

                cw_settings = node.agent.context_window
                strategy_name = cw_settings.compaction_strategy
                if strategy_name == "truncation":
                    strategy = TruncationStrategy()
                elif strategy_name == "llm_summarization":
                    # Use the runner's own provider for summarization calls
                    strategy = LLMSummarizationStrategy(provider=runner.provider)
                else:
                    # Default: observation_masking
                    strategy = ObservationMaskingStrategy()
                runner.context_tracker = ContextWindowTracker(
                    settings=cw_settings,
                    strategy=strategy,
                )

            enforcement_context = self._enforcement_context_for(run, node.node_id)
            try:
                result = await self._run_agent_with_optional_enforcement(
                    runner,
                    input_payload,
                    thread_id=thread_id,
                    runtime_context={"node_id": node.node_id, "run_id": run.run_id},
                    enforcement_context=enforcement_context,
                )
            finally:
                # Phase 37: Record context window state in audit before restoring.
                _ctx_tracker = getattr(runner, "context_tracker", None)
                if _ctx_tracker is not None and hasattr(_ctx_tracker, "state"):
                    _cw_state = _ctx_tracker.state
                    # Store for audit enrichment after the finally block.
                    _context_window_audit = {
                        "accumulated_tokens": _cw_state.accumulated_tokens,
                        "compaction_count": _cw_state.compaction_count,
                    }
                else:
                    _context_window_audit = None
                # Restore originals only if they existed on the runner.
                if original_provider is not _MISSING:
                    runner.provider = original_provider
                if original_memory_resolver is not _MISSING:
                    runner.memory_resolver = original_memory_resolver
                if original_budget_enforcer is not _MISSING:
                    runner.budget_enforcer = original_budget_enforcer
                # Phase 37: Restore original context tracker.
                if original_context_tracker is not _MISSING:
                    runner.context_tracker = original_context_tracker
                # Phase 36: Restore original config after template-based override.
                if effective_instruction is not None and original_config is not _MISSING:
                    runner.config = original_config

            audit_record = dict(result.audit_record)
            if enforcement_context:
                audit_record["enforcement"] = enforcement_context
                audit_record["enforcement_applied"] = True
            # Phase 36: Record template metadata in audit.
            if rendered_prompt_for_audit is not None:
                audit_record.setdefault("execution_metadata", {})
                audit_record["execution_metadata"]["rendered_prompt"] = rendered_prompt_for_audit
            if template_ref_for_audit is not None:
                audit_record.setdefault("execution_metadata", {})
                audit_record["execution_metadata"]["template_ref"] = template_ref_for_audit
            # Phase 37: Record context window state in audit.
            if _context_window_audit is not None:
                audit_record.setdefault("execution_metadata", {})
                audit_record["execution_metadata"]["context_window"] = _context_window_audit
            # Record template memory binding resolution in audit.
            if tmb_audit_records:
                audit_record.setdefault("execution_metadata", {})
                audit_record["execution_metadata"]["template_memory_bindings"] = tmb_audit_records
            return result.output_data, audit_record
        if isinstance(node, ExecutableUnitNode):
            enforcement_context = self._enforcement_context_for(run, node.node_id)
            if (
                self.secret_resolver is not None
                and getattr(
                    self.executable_unit_runner,
                    "secret_resolver",
                    None,
                )
                is None
            ):
                self.executable_unit_runner.secret_resolver = self.secret_resolver
            result = await self._run_executable_unit_with_optional_enforcement(
                node.executable_unit.manifest_ref,
                input_payload,
                enforcement_context=enforcement_context,
            )
            audit_record = dict(result.audit_record)
            if enforcement_context:
                audit_record["enforcement"] = enforcement_context
                audit_record["enforcement_applied"] = True
            return result.output_data, audit_record
        raise NodeDispatcherError(f"unsupported node type: {type(node)!r}")

    async def _resolve_thread(self, node: AgentNode, run: Run) -> str | None:
        """Figure out which thread ID an agent node should use.

        Some agents participate in threads (conversations), others don't.
        This checks the agent's configuration and uses the thread resolver
        to find or create the right thread.
        """
        mode = node.agent.thread_participation
        persistence_mode = node.agent.state_persistence.get("mode")
        if mode == "none" and persistence_mode != "thread":
            return None
        if self.thread_resolver is not None:
            resolution = await self.thread_resolver.resolve(
                run.thread_id,
                graph_version_ref=run.graph_version_ref,
                deployment_ref=run.deployment_ref,
                tenant_id=run.tenant_id,
                workspace_id=run.workspace_id,
                participating_agent_refs=[node.node_id],
                run_id=run.run_id,
            )
            run.thread_id = resolution.thread.thread_id
        return run.thread_id

    async def _resolve_template_memory(
        self,
        node: AgentNode,
        run: Run,
        thread_id: str | None,
        input_payload: Mapping[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Fetch memory values declared in ``template_memory_bindings``.

        Returns ``(memory_namespace, audit_records)`` where *memory_namespace*
        is the dict that populates ``{{ memory.* }}`` in prompt templates and
        *audit_records* is a list of per-binding audit dicts appended to the
        node audit record under ``execution_metadata.template_memory_bindings``.

        Raises ``MemoryBindingResolutionError`` when a connector is unknown or
        when a read/search call fails.
        """
        bindings = node.agent.template_memory_bindings
        if not bindings or self.memory_resolver is None:
            return {}, []

        from governai.memory.models import MemoryScope

        _scope_map = {
            "run": MemoryScope.RUN,
            "thread": MemoryScope.THREAD,
            "shared": MemoryScope.SHARED,
        }

        # Only resolve the connector refs actually used by template bindings.
        refs_needed = list({b.connector_instance_id for b in bindings})
        runtime_context: dict[str, Any] = {"node_id": node.node_id, "run_id": run.run_id}
        try:
            resolved = await self.memory_resolver.resolve(
                refs_needed,
                thread_id=thread_id,
                runtime_context=runtime_context,
                node_id=node.node_id,
            )
        except KeyError as exc:
            raise MemoryBindingResolutionError(
                f"unknown memory connector referenced in template_memory_bindings: {exc}"
            ) from exc

        connector_by_ref: dict[str, Any] = {rb.memory_ref: rb.connector for rb in resolved}
        state: dict[str, Any] = dict(run.metadata) if run.metadata else {}
        input_dict: dict[str, Any] = dict(input_payload)

        memory_ns: dict[str, Any] = {}
        audit_records: list[dict[str, Any]] = []

        for binding in bindings:
            connector = connector_by_ref.get(binding.connector_instance_id)
            if connector is None:
                raise MemoryBindingResolutionError(
                    f"connector '{binding.connector_instance_id}' not found in memory_refs; "
                    "add it to agent.memory_refs before using it in template_memory_bindings"
                )

            scope = _scope_map[binding.scope]

            try:
                if binding.access_mode == "get":
                    resolved_key = _substitute_tmb_key(
                        binding.key or "",
                        input_payload=input_dict,
                        state=state,
                        run_id=run.run_id,
                    )
                    entry = await connector.read(resolved_key, scope)
                    value = entry.value if entry is not None else binding.default
                    audit_records.append(
                        {
                            "as_name": binding.as_name,
                            "connector_instance_id": binding.connector_instance_id,
                            "access_mode": "get",
                            "key": resolved_key,
                            "scope": binding.scope,
                            "found": entry is not None,
                        }
                    )
                else:  # scan
                    prefix = binding.key_prefix or ""
                    if prefix:
                        prefix = _substitute_tmb_key(
                            prefix,
                            input_payload=input_dict,
                            state=state,
                            run_id=run.run_id,
                        )
                    all_entries = await connector.search({}, scope)
                    items: dict[str, Any] = {
                        entry.key[len(prefix) :]: entry.value
                        for entry in all_entries
                        if entry.key.startswith(prefix)
                    }
                    if binding.max_items is not None:
                        items = dict(list(items.items())[: binding.max_items])
                    value = items if items else binding.default
                    audit_records.append(
                        {
                            "as_name": binding.as_name,
                            "connector_instance_id": binding.connector_instance_id,
                            "access_mode": "scan",
                            "key_prefix": prefix,
                            "scope": binding.scope,
                            "item_count": len(items),
                        }
                    )
            except MemoryBindingResolutionError:
                raise
            except Exception as exc:
                raise MemoryBindingResolutionError(
                    f"failed to read memory binding '{binding.as_name}' "
                    f"(connector={binding.connector_instance_id}): {exc}"
                ) from exc

            memory_ns[binding.as_name] = value

        return memory_ns, audit_records

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

    async def _record_history(
        self,
        run: Run,
        node: Node,
        node_id: str,
        input_payload: Mapping[str, Any],
        output_payload: Mapping[str, Any],
        audit_record: Mapping[str, Any],
    ) -> None:
        """Save a record of this node's execution to the run history and audit log.

        Creates an audit entry (if an audit repository is configured) and
        appends a history entry to the run so you can see what happened
        at each step.
        """
        redacted_input = self._redact_for_audit(dict(input_payload))
        redacted_output = self._redact_for_audit(dict(output_payload))
        redacted_audit_record = self._redact_for_audit(dict(audit_record))
        audit_refs = list(run.audit_refs)
        audit_ref = f"audit:{len(audit_refs) + 1}"
        audit_refs.append(audit_ref)
        run.audit_refs = audit_refs
        if self.audit_repository is not None:
            # Promote token_usage and cost fields from runner audit record
            # to top-level NodeAuditRecord fields for queryability.
            token_usage_data = redacted_audit_record.get("token_usage")
            token_usage = (
                TokenUsage.model_validate(token_usage_data)
                if token_usage_data is not None
                else None
            )
            await self.audit_repository.write(
                NodeAuditRecord(
                    audit_id=self._stored_audit_id(run.run_id, audit_ref),
                    run_id=run.run_id,
                    thread_id=run.thread_id,
                    node_id=node_id,
                    node_version=node.node_version,
                    graph_version_ref=run.graph_version_ref,
                    deployment_ref=run.deployment_ref,
                    attempt=1,
                    status="completed",
                    input_snapshot=redacted_input,
                    output_snapshot=redacted_output,
                    execution_metadata=redacted_audit_record,
                    token_usage=token_usage,
                    cost_usd=redacted_audit_record.get("cost_usd"),
                    cost_event_id=redacted_audit_record.get("cost_event_id"),
                )
            )
        run.execution_history.append(
            RunHistoryEntry(
                node_id=node_id,
                status="completed",
                input_snapshot=redacted_input,
                output_snapshot=redacted_output,
                audit_ref=audit_ref,
            )
        )
        run.completed_steps = [entry.node_id for entry in run.execution_history]

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
    ) -> None:
        """Persist an audit record for execution failures that happen before completion."""
        audit_record = getattr(error, "audit_record", None)
        if self.audit_repository is None or not isinstance(audit_record, Mapping):
            return
        audit_refs = list(run.audit_refs)
        audit_ref = f"audit:{len(audit_refs) + 1}"
        audit_refs.append(audit_ref)
        run.audit_refs = audit_refs
        await self.audit_repository.write(
            NodeAuditRecord(
                audit_id=self._stored_audit_id(run.run_id, audit_ref),
                run_id=run.run_id,
                thread_id=run.thread_id,
                node_id=node_id,
                node_version=node.node_version,
                graph_version_ref=run.graph_version_ref,
                deployment_ref=run.deployment_ref,
                attempt=1,
                status="rejected",
                input_snapshot=self._redact_for_audit(dict(input_payload)),
                output_snapshot={},
                execution_metadata=self._redact_for_audit(dict(audit_record)),
                error=str(error),
            )
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

    async def _enforce_loop_guards(
        self,
        graph: Graph,
        run: Run,
        started_at: float,
    ) -> Run | None:
        """Check if the run has exceeded its step or time limits.

        Returns a failed Run if a limit is exceeded, or None if everything
        is within bounds. This prevents infinite loops in graphs.
        """
        total_steps = len(run.execution_history)
        settings = graph.execution_settings
        if total_steps >= settings.max_total_steps:
            return await self._fail_run(run, "max_total_steps", "max total step limit exceeded")
        if settings.max_total_runtime_seconds is not None:
            elapsed = perf_counter() - started_at
            if elapsed > settings.max_total_runtime_seconds:
                return await self._fail_run(run, "max_total_runtime", "max total runtime exceeded")
        return None

    async def _enforce_policy(
        self,
        graph: Graph,
        run: Run,
        node: Node,
        input_payload: Mapping[str, Any],
    ) -> Run | None:
        """Check if the policy guard allows this node to run.

        If a policy guard is configured and denies execution, the run is
        marked as failed with a policy violation reason. Returns None if
        no guard is set or if the policy allows execution.
        """
        guard = self.policy_guard
        if guard is None:
            return None
        result = guard.evaluate(graph, node, run, input_payload)
        if result.decision is PolicyDecision.ALLOW:
            enforcement = dict(run.metadata.get("enforcement", {}))
            enforcement[node.node_id] = result.model_dump(mode="json")
            run.metadata["enforcement"] = enforcement
            return None

        # Policy failures are recorded like a node attempt so operators can diagnose why it stopped.
        audit_refs = list(run.audit_refs)
        audit_ref = f"audit:{len(audit_refs) + 1}"
        audit_refs.append(audit_ref)
        run.audit_refs = audit_refs
        if self.audit_repository is not None:
            await self.audit_repository.write(
                NodeAuditRecord(
                    audit_id=self._stored_audit_id(run.run_id, audit_ref),
                    run_id=run.run_id,
                    thread_id=run.thread_id,
                    node_id=node.node_id,
                    node_version=node.node_version,
                    graph_version_ref=run.graph_version_ref,
                    deployment_ref=run.deployment_ref,
                    attempt=1,
                    status="rejected",
                    input_snapshot=self._redact_for_audit(dict(input_payload)),
                    output_snapshot={},
                    execution_metadata=self._redact_for_audit(
                        {
                            "enforcement": result.model_dump(mode="json"),
                            "enforcement_applied": False,
                        }
                    ),
                    error=result.reason,
                )
            )
        run.touch()
        run = await self.run_repository.put(run)
        return await self._fail_run(
            run, "policy_violation", result.reason or "policy denied execution"
        )

    def _enforcement_context_for(self, run: Run, node_id: str) -> dict[str, Any]:
        """Return the stored policy enforcement context for a node, if any."""
        enforcement = run.metadata.get("enforcement", {})
        if not isinstance(enforcement, Mapping):
            return {}
        context = enforcement.get(node_id, {})
        if not isinstance(context, Mapping):
            return {}
        return dict(context)

    async def _gate_policy_required_side_effects(
        self,
        run: Run,
        node: Node,
        input_payload: Mapping[str, Any],
    ) -> Run | None:
        """Pause execution when policy requires approval before side effects."""
        enforcement = self._enforcement_context_for(run, node.node_id)
        if not enforcement.get("approval_required_for_side_effects"):
            return None
        approved_nodes = set(run.metadata.get("approved_side_effect_nodes", []))
        if node.node_id in approved_nodes:
            return None
        if not self._node_has_side_effects(node):
            return None
        service = self.approval_service
        approval_id = None
        if service is not None:
            approval = await service.create_pending(
                run=run,
                node=HumanApprovalNode(
                    node_id=node.node_id,
                    graph_version_ref=node.graph_version_ref,
                    human_approval=HumanApprovalNodeData(),
                ),
                input_payload=dict(input_payload),
            )
            approval_id = approval.approval_id
        run.status = RunStatus.WAITING_APPROVAL
        payloads = dict(run.metadata.get("node_payloads", {}))
        payloads[node.node_id] = dict(input_payload)
        run.metadata["node_payloads"] = payloads
        run.metadata["pending_approval"] = {
            "node_id": node.node_id,
            "input": dict(input_payload),
            "approval_id": approval_id,
            "kind": "side_effect_policy",
        }
        run.pending_node_ids.insert(0, node.node_id)
        run.touch()
        persisted = await self.run_repository.put(run)
        await self.run_repository.write_checkpoint(persisted)
        await self._refresh_artifact_ttls(persisted)
        return persisted

    async def _consume_side_effect_approval(
        self,
        run: Run,
        node: Node,
        input_payload: Mapping[str, Any],
    ) -> Run | None:
        """Resolve pending side-effect approval state before re-executing a node."""
        pending = run.metadata.get("pending_approval")
        if not isinstance(pending, Mapping):
            return None
        if pending.get("kind") != "side_effect_policy" or pending.get("node_id") != node.node_id:
            return None
        approval_id = pending.get("approval_id")
        if approval_id is None or self.approval_service is None:
            run.status = RunStatus.WAITING_APPROVAL
            run.pending_node_ids.insert(0, node.node_id)
            persisted = await self.run_repository.put(run)
            await self.run_repository.write_checkpoint(persisted)
            await self._refresh_artifact_ttls(persisted)
            return persisted
        record = await self.approval_service.get(approval_id)
        if record is None or record.resolution is None:
            run.status = RunStatus.WAITING_APPROVAL
            run.pending_node_ids.insert(0, node.node_id)
            persisted = await self.run_repository.put(run)
            await self.run_repository.write_checkpoint(persisted)
            await self._refresh_artifact_ttls(persisted)
            return persisted
        run.metadata.pop("pending_approval", None)
        if record.resolution.decision is ApprovalDecision.REJECT:
            return await self._fail_run(run, "approval_rejected", "approval rejected")
        approved_nodes = set(run.metadata.get("approved_side_effect_nodes", []))
        approved_nodes.add(node.node_id)
        run.metadata["approved_side_effect_nodes"] = sorted(approved_nodes)
        if record.resolution.edited_payload is not None:
            payloads = dict(run.metadata.get("node_payloads", {}))
            payloads[node.node_id] = dict(record.resolution.edited_payload)
            run.metadata["node_payloads"] = payloads
        return None

    def _node_has_side_effects(self, node: Node) -> bool:
        """Detect whether a node can cause side effects that require approval."""
        if isinstance(node, ExecutableUnitNode):
            if bool(node.execution_config.get("side_effect")):
                return True
            registry = getattr(self.executable_unit_runner, "registry", None)
            if registry is not None and registry.has(node.executable_unit.manifest_ref):
                return bool(registry.get(node.executable_unit.manifest_ref).manifest.side_effect)
            return False
        if isinstance(node, AgentNode):
            runner = self.agent_runners.get(node.node_id)
            if runner is None:
                return False
            config = getattr(runner, "config", None)
            attachments = getattr(config, "tool_attachments", []) if config is not None else []
            return any(attachment.side_effect_allowed for attachment in attachments)
        return False

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
        parameters = inspect.signature(runner.run).parameters
        if "enforcement_context" in parameters:
            return await runner.run(
                input_payload,
                thread_id=thread_id,
                runtime_context=runtime_context,
                enforcement_context=enforcement_context,
            )
        return await runner.run(
            input_payload,
            thread_id=thread_id,
            runtime_context=runtime_context,
        )

    async def _run_executable_unit_with_optional_enforcement(
        self,
        manifest_ref: str,
        input_payload: Mapping[str, Any],
        *,
        enforcement_context: Mapping[str, Any],
    ) -> Any:
        """Call executable-unit runners with enforcement context when supported."""
        parameters = inspect.signature(self.executable_unit_runner.run).parameters
        if "enforcement_context" in parameters:
            return await self.executable_unit_runner.run(
                manifest_ref,
                input_payload,
                enforcement_context=enforcement_context,
            )
        return await self.executable_unit_runner.run(manifest_ref, input_payload)

    def _redact_for_audit(self, value: Any) -> Any:
        """Redact any resolved secret values before persisting audit material."""
        resolver = self.secret_resolver
        if resolver is None:
            return value
        return resolver.redactor().redact(value)

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
        return f"{run_id}:{audit_ref}"

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
        """Find the edge connecting two nodes, or None if there isn't one."""
        for edge in graph.edges:
            if edge.source_node_id == source_node_id and edge.target_node_id == target_node_id:
                return edge
        return None
