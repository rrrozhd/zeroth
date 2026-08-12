"""Durable structured-token execution adapter for :class:`GraphDriver`.

The legacy driver remains the compatibility implementation for flag-off runs.
This coordinator owns the flag-on queue and never reconstructs work from
``Run.pending_node_ids`` or node-keyed metadata.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from functools import partial
from time import perf_counter
from typing import Any, cast

from pydantic import JsonValue

from zeroth.contracts.governed import RunStatus
from zeroth.contracts.graph import Graph, HumanApprovalNode, SubgraphNode
from zeroth.contracts.graph.token_snapshot import TokenEngineSnapshot, TokenEngineSnapshotState
from zeroth.contracts.graph.tokens import (
    DispatchLifecycleState,
    IterationFrameState,
    JoinLifecycleState,
    PayloadDelivery,
)
from zeroth.contracts.mappings.executor import _set_path
from zeroth.platform.observability.tracing import start_span
from zeroth.runtime.orchestration import token_scope as _ts
from zeroth.runtime.orchestration.dispatcher import (
    SideEffectReconciliationExhaustedError,
    dispatch_subgraph_node,
)
from zeroth.runtime.orchestration.errors import OrchestratorError
from zeroth.runtime.orchestration.parallel_executor import sum_run_cost
from zeroth.runtime.orchestration.token_lifecycle import (
    TokenLifecycleAdapter,
    has_pending_structured_owner_work,
)
from zeroth.runtime.orchestration.token_loop_models import LoopReductionClaim
from zeroth.runtime.orchestration.token_loops import close_ready_loop
from zeroth.runtime.orchestration.token_runtime_loops import TokenRuntimeLoopSupport
from zeroth.runtime.orchestration.token_runtime_support import (
    TokenRuntimeSupport,
    TokenRuntimeUnsupportedError,
)
from zeroth.runtime.orchestration.token_scheduler import (
    DispatchClaim,
    FanOutBranch,
    claim_next_token,
    complete_dispatch,
    enqueue_dispatch,
    fail_dispatch,
    initialize_token_snapshot,
    recover_dispatch,
)
from zeroth.runtime.orchestration.token_snapshot_store import (
    TokenSnapshotConcurrencyError,
    TokenSnapshotStore,
)
from zeroth.runtime.orchestration.tool_executor import node_by_id
from zeroth.runtime.parallel.models import BranchContext
from zeroth.runtime.parallel.reducers import dispatch_strategy
from zeroth.runtime.runs import Run


class TokenRuntimeCoordinator(TokenRuntimeLoopSupport, TokenRuntimeSupport):
    """Coordinates durable token claims with the existing governed dispatch path."""

    def __init__(self, driver: Any, store: TokenSnapshotStore) -> None:
        self.driver = driver
        self.store = store
        self._fanout_spans: dict[str, Any] = {}

    @staticmethod
    def _branch_context(run: Run, envelope: Any) -> BranchContext:
        branch_index = envelope.fork_lineage[-1].child_ordinal
        return BranchContext(
            branch_index=branch_index,
            branch_id=f"{run.run_id}:branch:{branch_index}",
            input_payload=dict(envelope.payload),
        )

    def _increment_node_visit(self, run: Run, node_id: str, envelope: Any) -> None:
        if not envelope.fork_lineage:
            self.driver.increment_node_visit(run, node_id)
            return
        fork_id = envelope.fork_lineage[-1].fork_id
        visits = dict(run.metadata.get("token_fork_node_visits", {}))
        visited = set(visits.get(fork_id, []))
        if node_id in visited:
            return
        self.driver.increment_node_visit(run, node_id)
        visited.add(node_id)
        visits[fork_id] = sorted(visited)
        run.metadata["token_fork_node_visits"] = visits

    async def drive(self, graph: Graph, run: Run, *, step_tracker: Any = None) -> Run:
        try:
            return await self._drive(graph, run, step_tracker=step_tracker)
        finally:
            self._close_all_fanout_spans()

    async def _drive(self, graph: Graph, run: Run, *, step_tracker: Any = None) -> Run:
        del step_tracker  # token scheduling owns the aggregate work queue
        started_at = perf_counter()
        await self._ensure_snapshot(graph, run)
        while True:
            snapshot = await self.store.get_token_snapshot(run.run_id)
            if snapshot is None:
                raise OrchestratorError("token snapshot disappeared after initialization")
            lifecycle_stop = await self._settle_cancellation_requests(run, snapshot)
            if lifecycle_stop is not None:
                return lifecycle_stop
            if snapshot.state in {
                TokenEngineSnapshotState.PAUSED,
                TokenEngineSnapshotState.STOPPED,
                TokenEngineSnapshotState.CANCELLED,
            }:
                stopped = await self.driver.external_stop(run)
                if stopped is None:
                    raise OrchestratorError(
                        f"token snapshot is {snapshot.state.value} without a persisted run stop"
                    )
                return stopped
            if snapshot.state is TokenEngineSnapshotState.STOPPING:
                drained = await self._drain_stopping_owner(graph, run, snapshot)
                if drained is not snapshot:
                    continue
                if snapshot.in_flight_dispatches:
                    claim = await self._recover(snapshot)
                elif any(
                    token.fork_lineage
                    or token.iteration_memberships
                    or token.continuation_parent_token_ids
                    for token in snapshot.queue
                ):
                    claim = await self._claim(snapshot)
                else:
                    claim = None
                if claim is not None:
                    terminal = await self._dispatch_claim(graph, run, claim)
                    if terminal is not None:
                        return terminal
                    continue
                await TokenLifecycleAdapter(self.store).stop(run.run_id)
                current = await self.store.get_token_snapshot(run.run_id)
                if current is not None and current.state is TokenEngineSnapshotState.STOPPING:
                    stopped = await self.driver.external_stop(run)
                    return stopped or run
                continue
            stopped = await self.driver.external_stop(run)
            if stopped is not None:
                return stopped
            fork_owned = any(
                token.fork_lineage and token.settled_revision is None for token in snapshot.tokens
            )
            failed = await self.driver.policy_gate.enforce_loop_guards(
                graph,
                run,
                started_at,
                failure_reason="parallel_execution_failed" if fork_owned else None,
            )
            if failed is not None:
                return failed
            if (
                self.driver.per_run_cap_usd is not None
                and sum_run_cost(run) >= self.driver.per_run_cap_usd
            ):
                spent = sum_run_cost(run)
                return await self.driver.fail_run(
                    run,
                    "node_execution_failed",
                    f"per-run budget exceeded: ${spent:.4f} >= ${self.driver.per_run_cap_usd:.4f}",
                )
            if snapshot.state is TokenEngineSnapshotState.COMPLETED:
                return await self._complete_run(run)
            if snapshot.in_flight_dispatches:
                claim = await self._recover(snapshot)
            elif snapshot.queue and (
                snapshot.state is TokenEngineSnapshotState.RUNNING
                or any(
                    token.fork_lineage or token.iteration_memberships for token in snapshot.queue
                )
            ):
                claim = await self._claim(snapshot)
            else:
                if any(token.settled_revision is None for token in snapshot.tokens):
                    raise OrchestratorError("token engine is non-terminal with an empty work queue")
                await self._mark_snapshot_completed(snapshot)
                continue
            terminal = await self._dispatch_claim(graph, run, claim)
            if terminal is not None:
                return terminal

    def _ensure_fanout_spans(
        self,
        graph: Graph,
        run: Run,
        snapshot: TokenEngineSnapshot,
        envelope: Any,
    ) -> None:
        for frame in envelope.fork_lineage:
            if frame.fork_id in self._fanout_spans:
                continue
            fork = next(item for item in snapshot.forks if item.fork_id == frame.fork_id)
            parent = next(item for item in snapshot.tokens if item.token_id == fork.parent_token_id)
            owner = node_by_id(graph, parent.current_node_id)
            span = start_span(
                "zeroth.fanout",
                {"zeroth.node_id": owner.node_id, "zeroth.run_id": run.run_id},
            )
            span.__enter__()
            self._fanout_spans[frame.fork_id] = span

    def _close_closed_fanout_spans(self, snapshot: TokenEngineSnapshot) -> None:
        for fork in reversed(snapshot.forks):
            if fork.lifecycle_state.value != "closed":
                continue
            span = self._fanout_spans.pop(fork.fork_id, None)
            if span is not None:
                span.__exit__(None, None, None)

    def _close_all_fanout_spans(self) -> None:
        for fork_id in reversed(tuple(self._fanout_spans)):
            self._fanout_spans.pop(fork_id).__exit__(None, None, None)

    async def _drain_stopping_owner(
        self,
        graph: Graph,
        run: Run,
        snapshot: TokenEngineSnapshot,
    ) -> TokenEngineSnapshot:
        ready_join = next(
            (
                join
                for join in snapshot.joins
                if join.lifecycle_state in {JoinLifecycleState.READY, JoinLifecycleState.REDUCING}
            ),
            None,
        )
        if ready_join is not None:
            edge = next(
                edge
                for edge in graph.edges
                if edge.target_node_id == ready_join.target_node_id
                and edge.edge_id in {item.inbound_edge_id for item in ready_join.obligations}
            )
            return await self._close_join_if_ready(
                graph,
                run,
                snapshot,
                edge,
                ready_join.provenance_tag,
            )
        ready_loop = next(
            (
                loop
                for loop in reversed(snapshot.loops)
                if loop.frames and loop.frames[-1].state is IterationFrameState.BARRIER_READY
            ),
            None,
        )
        if ready_loop is not None:
            header = node_by_id(graph, ready_loop.loop_header_node_id)
            return await self._transition(
                snapshot,
                partial(
                    close_ready_loop,
                    loop_instance_id=ready_loop.loop_instance_id,
                    continuation_config=getattr(header, "join_config", None),
                    claimed_reduction=(
                        LoopReductionClaim.from_loop(ready_loop)
                        if ready_loop.reduction_claim_id is not None
                        else None
                    ),
                ),
            )
        if has_pending_structured_owner_work(snapshot):
            return snapshot
        return snapshot

    async def _ensure_snapshot(self, graph: Graph, run: Run) -> TokenEngineSnapshot:
        current = await self.store.get_token_snapshot(run.run_id)
        if current is not None:
            return current
        payload = cast(JsonValue, run.metadata.get("initial_input", {}))
        proposed = initialize_token_snapshot(
            run_id=run.run_id,
            root_node_id=self.driver.entry_step(graph),
            payload=payload,
            failure_mode=graph.execution_settings.failure_policy,
        )
        try:
            return await self.store.compare_and_swap_token_snapshot(
                run.run_id, expected_revision=None, snapshot=proposed
            )
        except TokenSnapshotConcurrencyError:
            loaded = await self.store.get_token_snapshot(run.run_id)
            if loaded is None:
                raise
            return loaded

    async def _claim(self, snapshot: TokenEngineSnapshot) -> DispatchClaim:
        current = snapshot
        while True:
            claim = claim_next_token(current)
            try:
                committed = await self.store.compare_and_swap_token_snapshot(
                    current.run_id,
                    expected_revision=current.revision,
                    snapshot=claim.snapshot,
                )
                dispatch = next(
                    item
                    for item in committed.in_flight_dispatches
                    if item.dispatch_id == claim.dispatch.dispatch_id
                )
                return DispatchClaim(snapshot=committed, dispatch=dispatch)
            except TokenSnapshotConcurrencyError:
                loaded = await self.store.get_token_snapshot(current.run_id)
                if loaded is None:
                    raise OrchestratorError(
                        "token snapshot disappeared during queue claim"
                    ) from None
                current = loaded

    async def _recover(self, snapshot: TokenEngineSnapshot) -> DispatchClaim:
        current = snapshot
        dispatch_id = snapshot.in_flight_dispatches[0].dispatch_id
        while True:
            claim = recover_dispatch(current, dispatch_id=dispatch_id)
            try:
                committed = await self.store.compare_and_swap_token_snapshot(
                    current.run_id,
                    expected_revision=current.revision,
                    snapshot=claim.snapshot,
                )
                dispatch = next(
                    item
                    for item in committed.in_flight_dispatches
                    if item.dispatch_id == dispatch_id
                )
                return DispatchClaim(snapshot=committed, dispatch=dispatch)
            except TokenSnapshotConcurrencyError:
                loaded = await self.store.get_token_snapshot(current.run_id)
                if loaded is None:
                    raise OrchestratorError("token snapshot disappeared during recovery") from None
                current = loaded

    async def _settle_fork_failure(self, run: Run, node: Any, exc: BaseException) -> Run:
        """End a failed fan-out the way the failure deserves.

        The fork path intercepts before the root handler, so without this an
        exhausted ambiguous side effect inside a fan-out still failed the run
        terminally. Kept as a helper rather than an inline branch because
        ``_dispatch_claim`` already sits at the complexity ceiling the commit
        gate enforces.
        """
        if isinstance(exc, SideEffectReconciliationExhaustedError):
            return await self.driver.pause_for_reconciliation(run, node.node_id, str(exc))
        return await self.driver.fail_run(run, "parallel_execution_failed", str(exc))

    async def _dispatch_claim(self, graph: Graph, run: Run, claim: DispatchClaim) -> Run | None:
        dispatch = claim.dispatch
        envelope = dispatch.token
        self._ensure_fanout_spans(graph, run, claim.snapshot, envelope)
        node = node_by_id(graph, envelope.current_node_id)
        payload = envelope.model_dump(mode="json")["payload"]
        scopes = self.driver._graph_scopes(graph)
        if (
            envelope.causal_inbound_edge_id in scopes.exit_owner
            and isinstance(payload, list)
            and all(isinstance(item, Mapping) for item in payload)
        ):
            payload = self.driver._merge_join_payloads(
                graph, envelope.current_node_id, [dict(item) for item in payload]
            )
        if not isinstance(payload, Mapping):
            payload = {"value": payload}
        input_payload = dict(payload)
        if (
            self._is_convergent(graph, node.node_id)
            and envelope.fork_lineage
            and (
                envelope.causal_inbound_edge_id is not None
                or envelope.continuation_parent_token_ids
            )
        ):
            fork = next(
                item
                for item in claim.snapshot.forks
                if item.fork_id == envelope.fork_lineage[-1].fork_id
            )
            routes = self._cohort_routes_if_reachable(graph, claim.snapshot, fork, node.node_id)
            if routes is not None:
                inbound_edge_id = envelope.causal_inbound_edge_id or routes[envelope.token_id]
                edge = next(item for item in graph.edges if item.edge_id == inbound_edge_id)
                await self._route_join(
                    graph,
                    run,
                    claim,
                    edge,
                    input_payload,
                    delivered=True,
                    precomputed_delivery=PayloadDelivery(payload=cast(JsonValue, input_payload)),
                )
                return None
        run.current_node_ids = [node.node_id]
        run.current_step = node.node_id
        run.metadata["token_dispatch"] = {
            "dispatch_id": dispatch.dispatch_id,
            "idempotency_key": dispatch.idempotency_key,
            "attempt": dispatch.attempt,
            "token_id": envelope.token_id,
        }
        run.metadata["in_flight_dispatch"] = {
            "node_id": node.node_id,
            "input_payload": input_payload,
            "token_tag": [
                [frame.loop_header_node_id, frame.iteration_index]
                for frame in envelope.provenance_tag
            ],
        }
        run.touch()
        await self.driver.run_repository.put(run)
        await self.driver.run_repository.write_checkpoint(run)
        node_started_at = datetime.now(UTC)
        approval_result = run.metadata.get("token_approval_result")
        if isinstance(node, HumanApprovalNode) and approval_result is None:
            approval_id = None
            if self.driver.approval_service is not None:
                approval = await self.driver.approval_service.create_pending(
                    run=run, node=node, input_payload=input_payload
                )
                approval_id = approval.approval_id
            run.status = RunStatus.WAITING_APPROVAL
            run.metadata["pending_approval"] = {
                "node_id": node.node_id,
                "input": input_payload,
                "approval_id": approval_id,
            }
            run.touch()
            persisted = await self.driver.run_repository.put(run)
            await self.driver.run_repository.write_checkpoint(persisted)
            return persisted
        if isinstance(node, HumanApprovalNode):
            if approval_result.get("node_id") != node.node_id:
                raise OrchestratorError("approval result targets a different token claim")
            input_payload = dict(approval_result.get("input", input_payload))
            output_data = dict(approval_result.get("output", {}))
            audit_record = dict(approval_result.get("audit", {}))
            run.metadata.pop("token_approval_result", None)
            run.metadata.pop("pending_approval", None)
        else:
            try:
                pending_side_effect = run.metadata.get("pending_approval")
                consuming_side_effect = (
                    isinstance(pending_side_effect, Mapping)
                    and pending_side_effect.get("kind") == "side_effect_policy"
                    and pending_side_effect.get("node_id") == node.node_id
                )
                pending_approval = await self.driver.policy_gate.consume_side_effect_approval(
                    run, node, input_payload
                )
                if pending_approval is not None:
                    return pending_approval
                if consuming_side_effect:
                    run.pending_node_ids = [
                        pending for pending in run.pending_node_ids if pending != node.node_id
                    ]
                if envelope.fork_lineage:
                    denial_reason = await self.driver.policy_gate.enforce_policy_for_branch(
                        graph, run, node, input_payload
                    )
                    if denial_reason is not None:
                        branch_index = envelope.fork_lineage[-1].child_ordinal
                        raise RuntimeError(
                            f"policy denied branch {branch_index} node {node.node_id}: "
                            f"{denial_reason}"
                        )
                else:
                    denial = await self.driver.policy_gate.enforce_policy(
                        graph, run, node, input_payload
                    )
                    if denial is not None:
                        return denial
                side_effect_gate = (
                    None
                    if envelope.fork_lineage
                    else await self.driver.policy_gate.gate_policy_required_side_effects(
                        run, node, input_payload
                    )
                )
                if side_effect_gate is not None:
                    return side_effect_gate
                if isinstance(node, SubgraphNode):
                    subgraph_result = await dispatch_subgraph_node(
                        executor=self.driver.subgraph_executor,
                        orchestrator=self.driver.orchestrator,
                        parent_graph=graph,
                        parent_run=run,
                        node=node,
                        input_payload=input_payload,
                        # Explicit, not defaulted: token scheduling owns the
                        # aggregate work queue (see ``_drive``), so there is no
                        # step tracker on this path to hand down.
                        step_tracker=None,
                    )
                    if subgraph_result.terminal_run is not None:
                        return subgraph_result.terminal_run
                    output_data = subgraph_result.output or {}
                    audit_record = subgraph_result.audit or {}
                else:
                    output_data, audit_record = await self.driver.node_dispatcher.dispatch(
                        node, run, input_payload, graph
                    )
            except Exception as exc:
                if envelope.fork_lineage:
                    branch_context = self._branch_context(run, envelope)
                    await self.driver.audit_recorder.record_failed_branch_execution(
                        run,
                        node,
                        node.node_id,
                        input_payload,
                        exc,
                        branch_context,
                    )
                    fork_id = envelope.fork_lineage[-1].fork_id
                    fork = next(item for item in claim.snapshot.forks if item.fork_id == fork_id)
                    parent = next(
                        item
                        for item in claim.snapshot.tokens
                        if item.token_id == fork.parent_token_id
                    )
                    owner = node_by_id(graph, parent.current_node_id)
                    owner_config = getattr(owner, "parallel_config", None)
                    failure_mode = (
                        owner_config.fail_mode
                        if owner_config is not None
                        else graph.execution_settings.failure_policy
                    )
                    committed = await self._transition(
                        claim.snapshot,
                        partial(
                            fail_dispatch,
                            dispatch_id=dispatch.dispatch_id,
                            attempt=dispatch.attempt,
                            cancellation_generation=dispatch.cancellation_generation,
                        ),
                    )
                    if failure_mode == "best_effort":
                        results = dict(run.metadata.get("token_fanout_results", {}))
                        fork_results = dict(results.get(fork_id, {}))
                        fork_results[envelope.token_id] = None
                        results[fork_id] = fork_results
                        closed = next(item for item in committed.forks if item.fork_id == fork_id)
                        if closed.lifecycle_state.value == "closed":
                            source = node_by_id(graph, parent.current_node_id)
                            config = source.parallel_config
                            if config is None:
                                raise OrchestratorError(
                                    "best-effort fork owner has no parallel configuration"
                                ) from exc
                            ordered = [fork_results[child.token_id] for child in closed.children]
                            reduced = dispatch_strategy(
                                config.merge_strategy,
                                ordered,
                                reducer_ref=config.reducer_ref,
                            )
                            merged: dict[str, Any] = {}
                            _set_path(merged, config.split_path, reduced)
                            run.metadata["last_output"] = merged
                            results.pop(fork_id, None)
                        if results:
                            run.metadata["token_fanout_results"] = results
                        else:
                            run.metadata.pop("token_fanout_results", None)
                        run.metadata.pop("token_dispatch", None)
                        run.metadata.pop("in_flight_dispatch", None)
                        run.current_node_ids = []
                        run.current_step = None
                        run.status = RunStatus.RUNNING
                        run.touch()
                        await self.driver.run_repository.put(run)
                        await self.driver.run_repository.write_checkpoint(run)
                        return None
                    await TokenLifecycleAdapter(self.store).cancel(run.run_id)
                    return await self._settle_fork_failure(run, node, exc)
                # The same terminal-vs-resumable decision the legacy driver
                # makes. This is the DEFAULT execution path, so routing it here
                # is what actually stops an exhausted ambiguous side effect from
                # stranding a run -- fixing only the legacy driver fixed the
                # path fewer runs take.
                return await self.driver._settle_failed_dispatch(
                    run, node, node.node_id, input_payload, exc, node_started_at
                )

        lifecycle_stop = await self._settle_cancellation_requests(run)
        if lifecycle_stop is not None:
            return lifecycle_stop

        if envelope.fork_lineage:
            branch_context = self._branch_context(run, envelope)
            audit_record = {
                **audit_record,
                "branch_id": branch_context.branch_id,
                "branch_index": branch_context.branch_index,
            }
        await self.driver.audit_recorder.record_history(
            run,
            node,
            node.node_id,
            input_payload,
            output_data,
            audit_record,
            started_at=node_started_at,
        )
        self._increment_node_visit(run, node.node_id, envelope)
        plan = self.driver.run_branch_planner(graph, run, node.node_id, output_data)
        active = [
            edge
            for edge in graph.edges
            if edge.kind != "tool" and edge.edge_id in plan.branch_resolution.active_edge_ids
        ]
        suppressed = [
            edge
            for edge in graph.edges
            if edge.kind != "tool" and edge.edge_id in plan.branch_resolution.suppressed_edge_ids
        ]
        back_edges = self.driver._back_edge_ids(graph)
        defer_loop_exits = any(edge.edge_id in back_edges for edge in active) or any(
            edge.edge_id not in scopes.exit_owner for edge in active
        )
        deferred_suppressed = tuple(
            edge
            for edge in suppressed
            if not (defer_loop_exits and edge.edge_id in scopes.exit_owner)
        )
        join_edges = [
            edge
            for edge in (*active, *deferred_suppressed)
            if self._is_convergent(graph, edge.target_node_id)
        ]
        loop_handled, committed = await self._route_loop_entry(
            graph, run, claim, active, output_data
        )
        if not loop_handled:
            loop_handled, committed = await self._route_loop_boundary(
                graph, run, claim, active, output_data
            )
        if not loop_handled and join_edges and not envelope.fork_lineage:
            unreachable = self._unreachable_inbound_sources(graph, join_edges[0].target_node_id)
            if unreachable:
                return await self.driver.fail_run(
                    run,
                    "join_deadlock",
                    f"sequential join barrier for {join_edges[0].target_node_id} "
                    "has unreachable inbound source(s): " + ", ".join(unreachable),
                )
        deferred_target = next(
            (
                edge.target_node_id
                for edge in (*active, *suppressed)
                if self._deferred_join_waiters(claim.snapshot, edge.target_node_id)
            ),
            None,
        )
        if loop_handled:
            transition = None
        elif deferred_target is not None:
            deferred_edge = next(
                edge for edge in (*active, *suppressed) if edge.target_node_id == deferred_target
            )
            edge_order = {edge.edge_id: index for index, edge in enumerate(graph.edges)}
            if deferred_edge in active:
                current_delivery = PayloadDelivery(
                    payload=cast(
                        JsonValue,
                        self.driver.edge_payload(
                            graph,
                            run,
                            node.node_id,
                            deferred_target,
                            output_data,
                            deferred_edge,
                        ),
                    )
                )
            else:
                current_delivery = None
            committed = await self._transition(
                claim.snapshot,
                partial(
                    self._close_deferred_join,
                    dispatch_id=dispatch.dispatch_id,
                    attempt=dispatch.attempt,
                    cancellation_generation=dispatch.cancellation_generation,
                    target_node_id=deferred_target,
                    inbound_edge_id=deferred_edge.edge_id,
                    current_delivery=current_delivery,
                    edge_order=edge_order,
                    merge_payloads=partial(
                        self.driver._merge_join_payloads, graph, deferred_target
                    ),
                ),
            )
            transition = None
        elif join_edges and envelope.fork_lineage:
            mixed_active = [edge for edge in active if edge not in join_edges]
            active_join = [edge for edge in join_edges if edge in active]
            suppressed_join = [edge for edge in join_edges if edge not in active]
            primary_join = next(
                (
                    edge
                    for edge in join_edges
                    if all(
                        other is edge
                        or self._reachable_inbound_edges(
                            graph, edge.target_node_id, other.target_node_id
                        )
                        for other in join_edges
                    )
                ),
                None,
            )
            if primary_join is not None:
                committed = claim.snapshot
                for deferred_edge in active_join:
                    if deferred_edge is primary_join:
                        continue
                    next_payload = self.driver.edge_payload(
                        graph,
                        run,
                        node.node_id,
                        deferred_edge.target_node_id,
                        output_data,
                        deferred_edge,
                    )
                    committed = await self._transition(
                        committed,
                        partial(
                            self._append_deferred_join_delivery,
                            target_node_id=deferred_edge.target_node_id,
                            inbound_edge_id=deferred_edge.edge_id,
                            payload=cast(JsonValue, next_payload),
                            dispatch_id=dispatch.dispatch_id,
                            attempt=dispatch.attempt,
                            cancellation_generation=dispatch.cancellation_generation,
                        ),
                    )
                for next_edge in mixed_active:
                    next_payload = self.driver.edge_payload(
                        graph,
                        run,
                        node.node_id,
                        next_edge.target_node_id,
                        output_data,
                        next_edge,
                    )
                    committed = await self._transition(
                        committed,
                        partial(
                            self._append_detached,
                            parent=envelope,
                            node_id=next_edge.target_node_id,
                            inbound_edge_id=next_edge.edge_id,
                            payload=cast(JsonValue, next_payload),
                        ),
                    )
                routed_claim = DispatchClaim(snapshot=committed, dispatch=dispatch)
                committed = await self._route_join(
                    graph,
                    run,
                    routed_claim,
                    primary_join,
                    output_data,
                    delivered=primary_join in active,
                )
                transition = None
            elif len(join_edges) == 1 and mixed_active and suppressed_join:
                committed = await self._route_join(
                    graph,
                    run,
                    claim,
                    suppressed_join[0],
                    output_data,
                    delivered=False,
                )
                for next_edge in mixed_active:
                    next_payload = self.driver.edge_payload(
                        graph,
                        run,
                        node.node_id,
                        next_edge.target_node_id,
                        output_data,
                        next_edge,
                    )
                    committed = await self._transition(
                        committed,
                        partial(
                            self._append_detached,
                            parent=envelope,
                            node_id=next_edge.target_node_id,
                            inbound_edge_id=next_edge.edge_id,
                            payload=cast(JsonValue, next_payload),
                        ),
                    )
                transition = None
            elif len(join_edges) != 1 or any(edge not in join_edges for edge in active):
                raise TokenRuntimeUnsupportedError(
                    "one token cannot both resolve a join obligation and publish other successors"
                )
            else:
                committed = await self._route_join(
                    graph,
                    run,
                    claim,
                    join_edges[0],
                    output_data,
                    delivered=join_edges[0] in active,
                )
                transition = None
        elif getattr(node, "parallel_config", None) is not None:
            branches = self._parallel_branches(graph, run, node, output_data, active)
            transition = partial(
                self._fan_out,
                dispatch_id=dispatch.dispatch_id,
                attempt=dispatch.attempt,
                cancellation_generation=dispatch.cancellation_generation,
                branches=branches,
            )
        elif not active:
            transition = partial(
                complete_dispatch,
                dispatch_id=dispatch.dispatch_id,
                attempt=dispatch.attempt,
                cancellation_generation=dispatch.cancellation_generation,
            )
        elif len(active) == 1:
            edge = active[0]
            next_payload = self.driver.edge_payload(
                graph, run, node.node_id, edge.target_node_id, output_data, edge
            )
            transition = partial(
                enqueue_dispatch,
                dispatch_id=dispatch.dispatch_id,
                attempt=dispatch.attempt,
                cancellation_generation=dispatch.cancellation_generation,
                next_node_id=edge.target_node_id,
                inbound_edge_id=edge.edge_id,
                payload=cast(JsonValue, next_payload),
            )
        else:
            branches = tuple(
                FanOutBranch(
                    node_id=edge.target_node_id,
                    inbound_edge_id=edge.edge_id,
                    payload=cast(
                        JsonValue,
                        self.driver.edge_payload(
                            graph, run, node.node_id, edge.target_node_id, output_data, edge
                        ),
                    ),
                )
                for edge in active
            )
            transition = partial(
                self._fan_out,
                dispatch_id=dispatch.dispatch_id,
                attempt=dispatch.attempt,
                cancellation_generation=dispatch.cancellation_generation,
                branches=branches,
            )
        if transition is not None:
            committed = await self._transition(claim.snapshot, transition)
            output_data = self._merge_closed_fanout(
                graph, run, claim.snapshot, envelope, output_data, committed
            )
            source_tag = self._source_trace_tag(run, envelope.token_id)
            trace_tags = dict(run.metadata.get("token_trace_tags", {}))
            for resolved_edge in active:
                target_tag = _ts.propagate_tag(
                    source_tag, resolved_edge, self.driver._graph_scopes(graph)
                )
                for queued in committed.queue:
                    if queued.causal_inbound_edge_id == resolved_edge.edge_id:
                        trace_tags[queued.token_id] = [list(item) for item in target_tag]
                if resolved_edge.edge_id in back_edges:
                    continue
                if resolved_edge in join_edges and envelope.fork_lineage:
                    continue
                resolved_payload = self.driver.edge_payload(
                    graph,
                    run,
                    node.node_id,
                    resolved_edge.target_node_id,
                    output_data,
                    resolved_edge,
                )
                self._trace_resolution(
                    run,
                    resolved_edge,
                    True,
                    resolved_payload,
                    envelope,
                    tag=target_tag,
                )
                self._trace_join_ready(
                    run,
                    resolved_edge.target_node_id,
                    resolved_payload,
                    envelope,
                    tag=target_tag,
                )
            for resolved_edge in suppressed:
                if resolved_edge.edge_id in back_edges:
                    continue
                if (
                    resolved_edge.edge_id in scopes.exit_owner
                    and resolved_edge not in deferred_suppressed
                ):
                    continue
                if resolved_edge in join_edges and envelope.fork_lineage:
                    continue
                target_tag = _ts.propagate_tag(
                    source_tag, resolved_edge, self.driver._graph_scopes(graph)
                )
                self._trace_resolution(run, resolved_edge, False, None, envelope, tag=target_tag)
                self._trace_suppressed_cascade(graph, run, resolved_edge.target_node_id, envelope)
            run.metadata["token_trace_tags"] = trace_tags
        run.metadata["last_output"] = output_data
        run.metadata.pop("token_dispatch", None)
        run.metadata.pop("in_flight_dispatch", None)
        stopped = await self.driver.external_stop(run)
        if stopped is not None and committed.state is not TokenEngineSnapshotState.STOPPING:
            return stopped
        if committed.state is TokenEngineSnapshotState.STOPPING:
            # The run-level interrupt remains persisted by ``external_stop``, but
            # already-owned structured work must continue until its durable
            # fork/join/loop frontier is settled.
            return None
        run.status = RunStatus.RUNNING
        run.current_node_ids = []
        run.current_step = None
        run.touch()
        await self.driver.run_repository.put(run)
        await self.driver.run_repository.write_checkpoint(run)
        await self.driver.refresh_artifact_ttls(run)
        return None

    async def _settle_cancellation_requests(
        self,
        run: Run,
        snapshot: TokenEngineSnapshot | None = None,
    ) -> Run | None:
        """Acknowledge durable cancellation fences before accepting completion."""
        current = snapshot or await self.store.get_token_snapshot(run.run_id)
        if current is None:
            raise OrchestratorError("token snapshot disappeared during lifecycle settlement")
        requested = tuple(
            dispatch
            for dispatch in current.in_flight_dispatches
            if dispatch.lifecycle_state is DispatchLifecycleState.CANCELLATION_REQUESTED
        )
        if not requested:
            return None
        fence = current.cancellation_fence
        if fence is None:
            raise OrchestratorError("cancellation-requested dispatch has no durable fence")
        lifecycle = TokenLifecycleAdapter(self.store)
        for dispatch in requested:
            await lifecycle.acknowledge(
                run.run_id,
                dispatch_id=dispatch.dispatch_id,
                cancellation_generation=fence.generation,
            )
        return await self.driver.external_stop(run) or run

    async def _transition(self, base, transition):
        current = base
        while True:
            proposed = transition(current)
            try:
                committed = await self.store.compare_and_swap_token_snapshot(
                    current.run_id,
                    expected_revision=current.revision,
                    snapshot=proposed,
                )
                self._close_closed_fanout_spans(committed)
                return committed
            except TokenSnapshotConcurrencyError:
                loaded = await self.store.get_token_snapshot(current.run_id)
                if loaded is None:
                    raise OrchestratorError(
                        "token snapshot disappeared during transition"
                    ) from None
                current = loaded

    async def _mark_snapshot_completed(self, snapshot: TokenEngineSnapshot) -> None:
        data = {name: getattr(snapshot, name) for name in type(snapshot).model_fields}
        data.update(
            revision=snapshot.revision + 1,
            state=TokenEngineSnapshotState.COMPLETED,
            queue=(),
            tokens=(),
            forks=(),
            joins=(),
            loops=(),
            deferred_join_deliveries=(),
            in_flight_dispatches=(),
        )
        proposed = TokenEngineSnapshot.model_validate(data)
        try:
            await self.store.compare_and_swap_token_snapshot(
                snapshot.run_id,
                expected_revision=snapshot.revision,
                snapshot=proposed,
            )
        except TokenSnapshotConcurrencyError:
            return
