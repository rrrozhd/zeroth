"""Graph runtime orchestrator.

This module contains the main engine that executes a graph of agent nodes.
It walks through the graph step by step, running each node, checking policies,
handling human approvals, recording audit trails, and persisting run state
so that executions can be resumed if interrupted.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from zeroth.agent_runtime import AgentRunner, RepositoryThreadResolver
from zeroth.approvals import ApprovalRecord, ApprovalService
from zeroth.audit import AuditRepository, NodeAuditRecord
from zeroth.conditions import NextStepPlanner
from zeroth.conditions.models import ConditionContext, TraversalState
from zeroth.execution_units import ExecutableUnitRunner
from zeroth.graph import AgentNode, ExecutableUnitNode, Graph, HumanApprovalNode, Node
from zeroth.mappings import MappingExecutor
from zeroth.policy import PolicyDecision, PolicyGuard
from zeroth.runs import Run, RunFailureState, RunHistoryEntry, RunRepository, RunStatus


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
    thread_resolver: RepositoryThreadResolver | None = None
    branch_planner: NextStepPlanner = NextStepPlanner()
    mapping_executor: MappingExecutor = MappingExecutor()

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
            thread_id=thread_id,
            current_node_ids=[],
            pending_node_ids=[self._entry_step(graph)],
            metadata=self._initial_metadata(graph, initial_input),
        )
        persisted = self.run_repository.create(run)
        persisted.status = RunStatus.RUNNING
        persisted.touch()
        persisted = self.run_repository.put(persisted)
        self.run_repository.write_checkpoint(persisted)
        return await self._drive(graph, persisted)

    async def resume_graph(self, graph: Graph, run_id: str) -> Run:
        """Resume an existing run that was paused or waiting for approval.

        Looks up the run by ID and continues driving the graph from where
        it left off. Raises KeyError if the run doesn't exist, or
        OrchestratorError if the run can't be resumed (e.g., already completed).
        """
        run = self.run_repository.get(run_id)
        if run is None:
            raise KeyError(run_id)
        if run.status not in {RunStatus.RUNNING, RunStatus.PENDING, RunStatus.WAITING_APPROVAL}:
            raise OrchestratorError(f"run {run_id} is not resumable from status {run.status}")
        return await self._drive(graph, run)

    async def _drive(self, graph: Graph, run: Run) -> Run:
        """Main loop that processes nodes one at a time until done.

        Keeps popping the next pending node, running it, planning the
        next steps, and repeating until there are no more nodes to run,
        or until a guard/policy/approval stops execution.
        """
        started_at = perf_counter()
        while True:
            failed_run = self._enforce_loop_guards(graph, run, started_at)
            if failed_run is not None:
                return failed_run
            if not run.pending_node_ids:
                # No more work is queued, so the run can be closed out as successful.
                run.status = RunStatus.COMPLETED
                run.current_node_ids = []
                run.final_output = run.metadata.get("last_output")
                run.touch()
                persisted = self.run_repository.put(run)
                self.run_repository.write_checkpoint(persisted)
                return persisted

            node_id = run.pending_node_ids.pop(0)
            node = self._node_by_id(graph, node_id)
            # Each node consumes the payload that was prepared for it by the previous step.
            input_payload = self._payload_for(run, node_id)
            run.current_node_ids = [node_id]
            run.current_step = node_id
            run.touch()
            run = self.run_repository.put(run)

            denial = self._enforce_policy(graph, run, node, input_payload)
            if denial is not None:
                return denial

            if isinstance(node, HumanApprovalNode):
                service = self.approval_service
                approval_id = None
                if service is not None:
                    # Store a separate approval record so a human can review it outside the run.
                    approval = service.create_pending(
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
                persisted = self.run_repository.put(run)
                self.run_repository.write_checkpoint(persisted)
                return persisted

            try:
                output_data, audit_record = await self._dispatch_node(node, run, input_payload)
            except Exception as exc:
                return self._fail_run(run, "node_execution_failed", str(exc))
            self._record_history(run, node, node_id, input_payload, output_data, audit_record)
            self._increment_node_visit(run, node_id)
            next_node_ids = self._plan_next_nodes(graph, run, node_id, output_data)
            self._queue_next_nodes(graph, run, node_id, output_data, next_node_ids)
            run.metadata["last_output"] = output_data
            run.status = RunStatus.RUNNING
            run.current_node_ids = []
            run.touch()
            run = self.run_repository.put(run)
            self.run_repository.write_checkpoint(run)

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
            thread_id = self._resolve_thread(node, run)
            result = await runner.run(
                input_payload,
                thread_id=thread_id,
                runtime_context={"node_id": node.node_id, "run_id": run.run_id},
            )
            return result.output_data, result.audit_record
        if isinstance(node, ExecutableUnitNode):
            result = await self.executable_unit_runner.run(
                node.executable_unit.manifest_ref,
                input_payload,
            )
            return result.output_data, result.audit_record
        raise NodeDispatcherError(f"unsupported node type: {type(node)!r}")

    def _resolve_thread(self, node: AgentNode, run: Run) -> str | None:
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
            resolution = self.thread_resolver.resolve(
                run.thread_id,
                graph_version_ref=run.graph_version_ref,
                deployment_ref=run.deployment_ref,
                participating_agent_refs=[node.node_id],
                run_id=run.run_id,
            )
            run.thread_id = resolution.thread.thread_id
        return run.thread_id

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
                payload = self.mapping_executor.execute(output_data, edge.mapping)
            payloads[target_node_id] = payload
            run.pending_node_ids.append(target_node_id)
        run.metadata["node_payloads"] = payloads

    def _record_history(
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
        audit_refs = list(run.audit_refs)
        audit_ref = f"audit:{len(audit_refs) + 1}"
        audit_refs.append(audit_ref)
        run.audit_refs = audit_refs
        if self.audit_repository is not None:
            self.audit_repository.write(
                NodeAuditRecord(
                    audit_id=audit_ref,
                    run_id=run.run_id,
                    thread_id=run.thread_id,
                    node_id=node_id,
                    node_version=node.node_version,
                    graph_version_ref=run.graph_version_ref,
                    deployment_ref=run.deployment_ref,
                    attempt=1,
                    status="completed",
                    input_snapshot=dict(input_payload),
                    output_snapshot=dict(output_payload),
                    execution_metadata=dict(audit_record),
                )
            )
        run.execution_history.append(
            RunHistoryEntry(
                node_id=node_id,
                status="completed",
                input_snapshot=dict(input_payload),
                output_snapshot=dict(output_payload),
                audit_ref=audit_ref,
            )
        )
        run.completed_steps = [entry.node_id for entry in run.execution_history]

    def _increment_node_visit(self, run: Run, node_id: str) -> None:
        """Bump the visit counter for this node by one."""
        run.node_visit_counts[node_id] = run.node_visit_counts.get(node_id, 0) + 1

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

    def _enforce_loop_guards(
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
            return self._fail_run(run, "max_total_steps", "max total step limit exceeded")
        if settings.max_total_runtime_seconds is not None:
            elapsed = perf_counter() - started_at
            if elapsed > settings.max_total_runtime_seconds:
                return self._fail_run(run, "max_total_runtime", "max total runtime exceeded")
        return None

    def _enforce_policy(
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
            return None

        # Policy failures are recorded like a node attempt so operators can diagnose why it stopped.
        audit_refs = list(run.audit_refs)
        audit_ref = f"audit:{len(audit_refs) + 1}"
        audit_refs.append(audit_ref)
        run.audit_refs = audit_refs
        if self.audit_repository is not None:
            self.audit_repository.write(
                NodeAuditRecord(
                    audit_id=audit_ref,
                    run_id=run.run_id,
                    thread_id=run.thread_id,
                    node_id=node.node_id,
                    node_version=node.node_version,
                    graph_version_ref=run.graph_version_ref,
                    deployment_ref=run.deployment_ref,
                    attempt=1,
                    status="rejected",
                    input_snapshot=dict(input_payload),
                    output_snapshot={},
                    execution_metadata={
                        "effective_capabilities": sorted(
                            capability.value for capability in result.effective_capabilities
                        ),
                        "allowed_secrets": list(result.allowed_secrets),
                        "network_mode": result.network_mode,
                        "sandbox_strictness_mode": result.sandbox_strictness_mode,
                    },
                    error=result.reason,
                )
            )
        run.touch()
        run = self.run_repository.put(run)
        return self._fail_run(run, "policy_violation", result.reason or "policy denied execution")

    def record_approval_resolution(
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
            "approver": approval_record.resolution.approver if approval_record.resolution else None,
        }
        self._record_history(
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
        run = self.run_repository.put(run)
        self.run_repository.write_checkpoint(run)
        return run

    def _fail_run(self, run: Run, reason: str, message: str) -> Run:
        """Mark a run as failed with the given reason and save it."""
        run.status = RunStatus.FAILED
        run.failure_state = RunFailureState(reason=reason, message=message)
        run.metadata["termination_reason"] = reason
        run.touch()
        persisted = self.run_repository.put(run)
        self.run_repository.write_checkpoint(persisted)
        return persisted

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
