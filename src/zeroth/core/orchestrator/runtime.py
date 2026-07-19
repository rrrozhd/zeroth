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
from datetime import datetime
from typing import Any

from zeroth.core.agent_runtime import AgentRunner, RepositoryThreadResolver
from zeroth.core.approvals import ApprovalRecord, ApprovalService
from zeroth.core.audit import AuditRepository
from zeroth.core.audit.models import MemoryAccessRecord, ToolCallRecord
from zeroth.core.conditions import NextStepPlanner
from zeroth.core.execution_units import ExecutableUnitRunner
from zeroth.core.graph import (
    AgentNode,
    Graph,
    HumanApprovalNode,
    Node,
    RetrievalNode,
)
from zeroth.core.mappings import MappingExecutor
from zeroth.core.parallel.executor import ParallelExecutor
from zeroth.core.parallel.models import (
    BranchContext,
    FanInResult,
    GlobalStepTracker,
)
from zeroth.core.policy import Capability, PolicyGuard
from zeroth.core.runs import Run, RunRepository, RunStatus
from zeroth.core.secrets import SecretResolver
from zeroth.platform.observability import start_span
from zeroth.runtime.orchestration.audit_recorder import RuntimeAuditRecorder
from zeroth.runtime.orchestration.dispatcher import NodeDispatcher
from zeroth.runtime.orchestration.driver import GraphDriver
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
from zeroth.runtime.orchestration.policy_gate import RuntimePolicyGate
from zeroth.runtime.orchestration.tool_executor import RuntimeToolExecutor, node_by_id

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

    @property
    def _driver(self) -> GraphDriver:
        """The state-machine collaborator, built from this orchestrator's own dependencies.

        ``orchestrator=self`` and ``resume_graph=self.resume_graph`` are handed
        over explicitly: ``SubgraphExecutor.execute`` takes the orchestrator by
        keyword as part of its published contract, and a paused child run is
        resumed through the public entry point so its run span opens identically.
        """
        return GraphDriver(
            run_repository=self.run_repository,
            audit_recorder=self._audit_recorder,
            node_dispatcher=self._node_dispatcher,
            policy_gate=self._policy_gate,
            parallel_runtime=self._parallel_runtime,
            branch_planner=self.branch_planner,
            mapping_executor=self.mapping_executor,
            approval_service=self.approval_service,
            subgraph_executor=self.subgraph_executor,
            webhook_service=self.webhook_service,
            artifact_store=self.artifact_store,
            per_run_cap_usd=self.per_run_cap_usd,
            orchestrator=self,
            resume_graph=self.resume_graph,
        )

    async def _refresh_artifact_ttls(self, run: Run) -> None:
        """Refresh TTLs on all artifact references found in run state."""
        await self._driver.refresh_artifact_ttls(run)

    async def _drive(
        self,
        graph: Graph,
        run: Run,
        *,
        step_tracker: GlobalStepTracker | None = None,
    ) -> Run:
        """Main loop that processes nodes one at a time until done."""
        return await self._driver.drive(graph, run, step_tracker=step_tracker)

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
        """Decide which nodes to run next based on the current node's output."""
        return self._driver.plan_next_nodes(graph, run, node_id, output_data)

    def _queue_next_nodes(
        self,
        graph: Graph,
        run: Run,
        source_node_id: str,
        output_data: Mapping[str, Any],
        next_node_ids: list[str],
    ) -> None:
        """Add the next nodes to the pending queue with their input payloads."""
        self._driver.queue_next_nodes(graph, run, source_node_id, output_data, next_node_ids)

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
        self._driver.increment_node_visit(run, node_id)

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
        """Get and remove the queued input payload for a node."""
        return self._driver.payload_for(run, node_id)

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
        return await self._driver.fail_run(run, reason, message)

    async def _emit_webhook(
        self,
        event_type: str,
        run: Run,
        data: dict[str, Any],
    ) -> None:
        """Emit a webhook event if a webhook service is configured."""
        await self._driver.emit_webhook(event_type, run, data)

    def _entry_step(self, graph: Graph) -> str:
        """Get the ID of the first node to run in the graph."""
        return self._driver.entry_step(graph)

    def _graph_version_ref(self, graph: Graph) -> str:
        """Build a version reference string like 'my-graph:v2'."""
        return self._driver.graph_version_ref(graph)

    def _stored_audit_id(self, run_id: str, audit_ref: str) -> str:
        """Namespace persisted audit IDs by run so append-only storage stays globally unique."""
        return RuntimeAuditRecorder.stored_audit_id(run_id, audit_ref)

    def _initial_metadata(self, graph: Graph, initial_input: Mapping[str, Any]) -> dict[str, Any]:
        """Build the starting metadata dict for a new run."""
        return self._driver.initial_metadata(graph, initial_input)

    def _node_by_id(self, graph: Graph, node_id: str) -> Node:
        """Find a node in the graph by its ID. Raises KeyError if not found."""
        return node_by_id(graph, node_id)

    def _edge_for(self, graph: Graph, source_node_id: str, target_node_id: str):
        """Find the data edge connecting two nodes, or None if there isn't one."""
        return self._driver.edge_for(graph, source_node_id, target_node_id)
