"""Durable structured-token execution adapter for :class:`GraphDriver`.

The legacy driver remains the compatibility implementation for flag-off runs.
This coordinator owns the flag-on queue and never reconstructs work from
``Run.pending_node_ids`` or node-keyed metadata.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from functools import partial
from typing import Any, cast

from pydantic import JsonValue

from zeroth.contracts.graph import Graph, HumanApprovalNode, SubgraphNode
from zeroth.contracts.graph.token_snapshot import TokenEngineSnapshot, TokenEngineSnapshotState
from zeroth.contracts.graph.tokens import DispatchLifecycleState
from zeroth.core.runs import Run, RunStatus
from zeroth.runtime.orchestration import token_scope as _ts
from zeroth.runtime.orchestration.dispatcher import dispatch_subgraph_node
from zeroth.runtime.orchestration.errors import OrchestratorError
from zeroth.runtime.orchestration.token_lifecycle import TokenLifecycleAdapter
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
    initialize_token_snapshot,
    recover_dispatch,
)
from zeroth.runtime.orchestration.token_snapshot_store import (
    TokenSnapshotConcurrencyError,
    TokenSnapshotStore,
)
from zeroth.runtime.orchestration.tool_executor import node_by_id


class TokenRuntimeCoordinator(TokenRuntimeLoopSupport, TokenRuntimeSupport):
    """Coordinates durable token claims with the existing governed dispatch path."""

    def __init__(self, driver: Any, store: TokenSnapshotStore) -> None:
        self.driver = driver
        self.store = store

    async def drive(self, graph: Graph, run: Run, *, step_tracker: Any = None) -> Run:
        del step_tracker  # token scheduling owns the aggregate work queue
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
            if snapshot.state is TokenEngineSnapshotState.COMPLETED:
                return await self._complete_run(run)
            if snapshot.in_flight_dispatches:
                claim = await self._recover(snapshot)
            elif snapshot.queue:
                claim = await self._claim(snapshot)
            else:
                if any(token.settled_revision is None for token in snapshot.tokens):
                    raise OrchestratorError("token engine is non-terminal with an empty work queue")
                await self._mark_snapshot_completed(snapshot)
                continue
            terminal = await self._dispatch_claim(graph, run, claim)
            if terminal is not None:
                return terminal

    async def _ensure_snapshot(self, graph: Graph, run: Run) -> TokenEngineSnapshot:
        current = await self.store.get_token_snapshot(run.run_id)
        if current is not None:
            return current
        payload = cast(JsonValue, run.metadata.get("initial_input", {}))
        proposed = initialize_token_snapshot(
            run_id=run.run_id,
            root_node_id=self.driver.entry_step(graph),
            payload=payload,
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

    async def _dispatch_claim(self, graph: Graph, run: Run, claim: DispatchClaim) -> Run | None:
        dispatch = claim.dispatch
        envelope = dispatch.token
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
            inbound_edge_id = envelope.causal_inbound_edge_id or self._continuation_inbound_edge(
                claim.snapshot, envelope.token_id
            )
            edge = next(item for item in graph.edges if item.edge_id == inbound_edge_id)
            await self._route_join(graph, run, claim, edge, input_payload, delivered=True)
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
                denial = await self.driver.policy_gate.enforce_policy(
                    graph, run, node, input_payload
                )
                if denial is not None:
                    return denial
                if isinstance(node, SubgraphNode):
                    subgraph_result = await dispatch_subgraph_node(
                        executor=self.driver.subgraph_executor,
                        orchestrator=self.driver.orchestrator,
                        parent_graph=graph,
                        parent_run=run,
                        node=node,
                        input_payload=input_payload,
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
                await self.driver.audit_recorder.record_failed_execution(
                    run, node, node.node_id, input_payload, exc, started_at=node_started_at
                )
                return await self.driver.fail_run(run, "node_execution_failed", str(exc))

        lifecycle_stop = await self._settle_cancellation_requests(run)
        if lifecycle_stop is not None:
            return lifecycle_stop

        await self.driver.audit_recorder.record_history(
            run,
            node,
            node.node_id,
            input_payload,
            output_data,
            audit_record,
            started_at=node_started_at,
        )
        self.driver.increment_node_visit(run, node.node_id)
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
        if loop_handled:
            transition = None
        elif join_edges and envelope.fork_lineage:
            mixed_active = [edge for edge in active if edge not in join_edges]
            suppressed_join = [edge for edge in join_edges if edge not in active]
            if len(join_edges) == 1 and mixed_active and suppressed_join:
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
                return await self.store.compare_and_swap_token_snapshot(
                    current.run_id,
                    expected_revision=current.revision,
                    snapshot=proposed,
                )
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
