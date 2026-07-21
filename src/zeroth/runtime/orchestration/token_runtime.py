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
from zeroth.core.runs import Run, RunStatus
from zeroth.runtime.orchestration.errors import OrchestratorError
from zeroth.runtime.orchestration.token_scheduler import (
    DispatchClaim,
    FanOutBranch,
    claim_next_token,
    complete_dispatch,
    enqueue_dispatch,
    fail_dispatch,
    fan_out_dispatch,
    initialize_token_snapshot,
    recover_dispatch,
)
from zeroth.runtime.orchestration.token_snapshot_store import (
    TokenSnapshotConcurrencyError,
    TokenSnapshotStore,
)
from zeroth.runtime.orchestration.tool_executor import node_by_id
from zeroth.runtime.parallel.errors import FanOutValidationError


class TokenRuntimeUnsupportedError(OrchestratorError):
    """A graph shape has no structured-token runtime adapter yet."""


class TokenRuntimeCoordinator:
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
        if isinstance(node, (HumanApprovalNode, SubgraphNode)):
            raise TokenRuntimeUnsupportedError(
                f"structured-token execution for {node.node_type} is not implemented"
            )
        if self._is_convergent(graph, node.node_id):
            raise TokenRuntimeUnsupportedError(
                f"structured-token join routing for node {node.node_id!r} is not implemented"
            )
        payload = envelope.model_dump(mode="json")["payload"]
        if not isinstance(payload, Mapping):
            payload = {"value": payload}
        input_payload = dict(payload)
        run.current_node_ids = [node.node_id]
        run.current_step = node.node_id
        run.metadata["token_dispatch"] = {
            "dispatch_id": dispatch.dispatch_id,
            "idempotency_key": dispatch.idempotency_key,
            "attempt": dispatch.attempt,
            "token_id": envelope.token_id,
        }
        run.touch()
        await self.driver.run_repository.put(run)
        await self.driver.run_repository.write_checkpoint(run)
        node_started_at = datetime.now(UTC)
        try:
            denial = await self.driver.policy_gate.enforce_policy(graph, run, node, input_payload)
            if denial is not None:
                return denial
            output_data, audit_record = await self.driver.node_dispatcher.dispatch(
                node, run, input_payload, graph
            )
        except Exception as exc:
            await self.driver.audit_recorder.record_failed_execution(
                run, node, node.node_id, input_payload, exc, started_at=node_started_at
            )
            await self._transition(
                claim.snapshot,
                lambda current: fail_dispatch(
                    current,
                    dispatch_id=dispatch.dispatch_id,
                    attempt=dispatch.attempt,
                    cancellation_generation=dispatch.cancellation_generation,
                ),
            )
            return await self.driver.fail_run(run, "node_execution_failed", str(exc))

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
        if getattr(node, "parallel_config", None) is not None:
            branches = self._parallel_branches(graph, run, node, output_data, active)
            transition = partial(
                fan_out_dispatch,
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
                fan_out_dispatch,
                dispatch_id=dispatch.dispatch_id,
                attempt=dispatch.attempt,
                cancellation_generation=dispatch.cancellation_generation,
                branches=branches,
            )
        await self._transition(claim.snapshot, transition)
        run.metadata["last_output"] = output_data
        run.metadata.pop("token_dispatch", None)
        run.status = RunStatus.RUNNING
        run.current_node_ids = []
        run.current_step = None
        run.touch()
        await self.driver.run_repository.put(run)
        await self.driver.run_repository.write_checkpoint(run)
        await self.driver.refresh_artifact_ttls(run)
        return None

    def _parallel_branches(self, graph, run, node, output, active):
        if not active:
            raise FanOutValidationError("parallel fan-out has no active downstream edge")
        contexts = self.driver.parallel_runtime.parallel_executor.split_fan_out(
            run.run_id, output, node.parallel_config, node
        )
        return tuple(
            FanOutBranch(
                node_id=edge.target_node_id,
                inbound_edge_id=edge.edge_id,
                payload=cast(JsonValue, dict(context.input_payload)),
            )
            for context in contexts
            for edge in active
        )

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

    async def _complete_run(self, run: Run) -> Run:
        run.status = RunStatus.COMPLETED
        run.current_node_ids = []
        run.current_step = None
        run.final_output = run.metadata.get("last_output")
        run.metadata.pop("token_dispatch", None)
        run.touch()
        persisted = await self.driver.run_repository.put(run)
        await self.driver.run_repository.write_checkpoint(persisted)
        await self.driver.refresh_artifact_ttls(persisted)
        await self.driver.emit_webhook(
            "run.completed",
            persisted,
            {"run_id": persisted.run_id, "status": "completed"},
        )
        return persisted

    @staticmethod
    def _is_convergent(graph: Graph, node_id: str) -> bool:
        return (
            sum(
                edge.enabled and edge.kind != "tool" and edge.target_node_id == node_id
                for edge in graph.edges
            )
            > 1
        )


__all__ = ["TokenRuntimeCoordinator", "TokenRuntimeUnsupportedError"]
