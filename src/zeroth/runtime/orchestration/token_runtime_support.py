"""Durable structured-token execution adapter for :class:`GraphDriver`.

The legacy driver remains the compatibility implementation for flag-off runs.
This coordinator owns the flag-on queue and never reconstructs work from
``Run.pending_node_ids`` or node-keyed metadata.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from functools import partial
from typing import Any, cast

from pydantic import JsonValue

from zeroth.contracts.graph import Graph
from zeroth.contracts.graph.token_snapshot import TokenEngineSnapshot
from zeroth.contracts.graph.tokens import (
    JoinLifecycleState,
    JoinObligationOutcome,
    SchedulingState,
    TokenEnvelope,
    TokenLifecycleState,
)
from zeroth.contracts.mappings.executor import _set_path
from zeroth.core.runs import Run, RunStatus
from zeroth.runtime.orchestration import token_scope as _ts
from zeroth.runtime.orchestration.errors import OrchestratorError
from zeroth.runtime.orchestration.token_joins import (
    close_ready_join_with_cas,
    deliver_to_join,
    settle_join_without_delivery,
)
from zeroth.runtime.orchestration.token_scheduler import (
    DispatchClaim,
    FanOutBranch,
    _stable_id,
    fan_out_dispatch,
)
from zeroth.runtime.orchestration.tool_executor import node_by_id
from zeroth.runtime.parallel.errors import FanOutValidationError
from zeroth.runtime.parallel.reducers import dispatch_strategy


class TokenRuntimeUnsupportedError(OrchestratorError):
    """A graph shape has no structured-token runtime adapter yet."""


class TokenRuntimeSupport:
    """Topology, join, fan-out, and compatibility support for the coordinator."""

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

    @staticmethod
    def _fan_out(snapshot: TokenEngineSnapshot, **command: Any) -> TokenEngineSnapshot:
        before_ids = {token.token_id for token in snapshot.queue}
        nested = bool(
            next(
                item
                for item in snapshot.in_flight_dispatches
                if item.dispatch_id == command["dispatch_id"]
            ).token.fork_lineage
        )
        proposed = fan_out_dispatch(snapshot, **command)
        if not nested:
            return proposed
        new_children = tuple(token for token in proposed.queue if token.token_id not in before_ids)
        retained = tuple(token for token in proposed.queue if token.token_id in before_ids)
        data = {name: getattr(proposed, name) for name in type(proposed).model_fields}
        data["queue"] = (*new_children, *retained)
        return TokenEngineSnapshot.model_validate(data)

    @staticmethod
    def _append_detached(
        snapshot: TokenEngineSnapshot,
        *,
        parent: TokenEnvelope,
        node_id: str,
        inbound_edge_id: str,
        payload: JsonValue,
    ) -> TokenEngineSnapshot:
        revision = snapshot.revision + 1
        token = TokenEnvelope(
            token_id=_stable_id("tok", snapshot.run_id, "detached", snapshot.next_token_ordinal),
            parent_token_id=parent.token_id,
            current_node_id=node_id,
            causal_inbound_edge_id=inbound_edge_id,
            payload=payload,
            lifecycle_state=TokenLifecycleState.ACTIVE,
            scheduling_state=SchedulingState.QUEUED,
            cancellation_generation=parent.cancellation_generation,
            state_revision=revision,
        )
        data = {name: getattr(snapshot, name) for name in type(snapshot).model_fields}
        data.update(
            revision=revision,
            next_token_ordinal=snapshot.next_token_ordinal + 1,
            queue=(*snapshot.queue, token),
            tokens=(*snapshot.tokens, token),
        )
        return TokenEngineSnapshot.model_validate(data)

    def _merge_closed_fanout(
        self,
        graph: Graph,
        run: Run,
        before: TokenEngineSnapshot,
        token: Any,
        output: dict[str, Any],
        after: TokenEngineSnapshot,
    ) -> dict[str, Any]:
        if not token.fork_lineage:
            return output
        fork_id = token.fork_lineage[-1].fork_id
        results = dict(run.metadata.get("token_fanout_results", {}))
        fork_results = dict(results.get(fork_id, {}))
        fork_results[token.token_id] = output
        results[fork_id] = fork_results
        run.metadata["token_fanout_results"] = results
        fork = next(item for item in after.forks if item.fork_id == fork_id)
        if fork.lifecycle_state.value != "closed":
            return output
        parent = next(item for item in before.tokens if item.token_id == fork.parent_token_id)
        source = node_by_id(graph, parent.current_node_id)
        config = getattr(source, "parallel_config", None)
        if config is None:
            return output
        ordered = [fork_results[child.token_id] for child in fork.children]
        reduced = dispatch_strategy(config.merge_strategy, ordered, reducer_ref=config.reducer_ref)
        merged: dict[str, Any] = {}
        _set_path(merged, config.split_path, reduced)
        results.pop(fork_id, None)
        if results:
            run.metadata["token_fanout_results"] = results
        else:
            run.metadata.pop("token_fanout_results", None)
        return merged

    async def _route_join(
        self,
        graph: Graph,
        run: Run,
        claim: DispatchClaim,
        edge: Any,
        output_data: Mapping[str, Any],
        *,
        delivered: bool,
    ) -> TokenEngineSnapshot:
        dispatch = claim.dispatch
        routes = self._cohort_routes(graph, claim.snapshot, dispatch.token, edge.target_node_id)
        if delivered:
            payload = self.driver.edge_payload(
                graph,
                run,
                edge.source_node_id,
                edge.target_node_id,
                output_data,
                edge,
            )
            transition = partial(
                deliver_to_join,
                target_node_id=edge.target_node_id,
                inbound_edge_id=edge.edge_id,
                cohort_inbound_edges=routes,
                payload=cast(JsonValue, payload),
                dispatch_id=dispatch.dispatch_id,
                attempt=dispatch.attempt,
                cancellation_generation=dispatch.cancellation_generation,
                failure_mode=graph.execution_settings.failure_policy,
            )
        else:
            payload = None
            transition = partial(
                settle_join_without_delivery,
                target_node_id=edge.target_node_id,
                inbound_edge_id=edge.edge_id,
                cohort_inbound_edges=routes,
                outcome=JoinObligationOutcome.SUPPRESSED,
                dispatch_id=dispatch.dispatch_id,
                attempt=dispatch.attempt,
                cancellation_generation=dispatch.cancellation_generation,
                failure_mode=graph.execution_settings.failure_policy,
            )
        committed = await self._transition(claim.snapshot, transition)
        source_tag = self._source_trace_tag(run, dispatch.token.token_id)
        target_tag = _ts.propagate_tag(source_tag, edge, self.driver._graph_scopes(graph))
        self._trace_resolution(run, edge, delivered, payload, dispatch.token, tag=target_tag)
        ready = next(
            (
                join
                for join in committed.joins
                if join.target_node_id == edge.target_node_id
                and join.lifecycle_state is JoinLifecycleState.READY
            ),
            None,
        )
        if ready is None:
            compatibility = dict(run.metadata.get("join_state", {}))
            compatibility[edge.target_node_id] = {"snapshot": committed.revision}
            run.metadata["join_state"] = compatibility
            return committed
        target = node_by_id(graph, edge.target_node_id)
        config = getattr(target, "join_config", None)
        if config is None:
            from zeroth.contracts.graph.models import JoinConfig

            config = JoinConfig(merge_strategy="collect")

        def reducer(_config: Any, inputs: tuple[Any, ...]) -> JsonValue:
            payloads = [item.payload for item in inputs]
            return cast(
                JsonValue,
                self.driver._merge_join_payloads(graph, edge.target_node_id, payloads),
            )

        closed = await close_ready_join_with_cas(
            self.store,
            run.run_id,
            ready.join_instance_id,
            config,
            reducer=reducer,
            failure_mode=graph.execution_settings.failure_policy,
        )
        compatibility = dict(run.metadata.get("join_state", {}))
        compatibility.pop(edge.target_node_id, None)
        if compatibility:
            run.metadata["join_state"] = compatibility
        else:
            run.metadata.pop("join_state", None)
        continuation = (
            next(token for token in closed.queue if token.token_id == ready.continuation_token_id)
            if ready.continuation_token_id is not None
            else closed.queue[-1]
        )
        reduced = continuation.model_dump(mode="json")["payload"]
        if isinstance(reduced, Mapping):
            self._trace_join_ready(
                run,
                edge.target_node_id,
                dict(reduced),
                continuation,
                tag=target_tag,
            )
        return closed

    def _cohort_routes(
        self,
        graph: Graph,
        snapshot: TokenEngineSnapshot,
        token: Any,
        target_node_id: str,
    ) -> dict[str, str]:
        fork_id = token.fork_lineage[-1].fork_id
        fork = next(item for item in snapshot.forks if item.fork_id == fork_id)
        tokens = {item.token_id: item for item in snapshot.tokens}
        routes: dict[str, str] = {}
        for child in fork.children:
            child_token = tokens[child.token_id]
            inbound = (
                [child_token.causal_inbound_edge_id]
                if child_token.current_node_id == target_node_id
                and child_token.causal_inbound_edge_id is not None
                else [self._continuation_inbound_edge(snapshot, child_token.token_id)]
                if child_token.current_node_id == target_node_id
                and child_token.continuation_parent_token_ids
                else self._reachable_inbound_edges(
                    graph, child_token.current_node_id, target_node_id
                )
            )
            if len(inbound) != 1:
                raise TokenRuntimeUnsupportedError(
                    f"fork child {child.token_id!r} has {len(inbound)} routes to join "
                    f"{target_node_id!r}"
                )
            routes[child.token_id] = inbound[0]
        return routes

    @staticmethod
    def _continuation_inbound_edge(
        snapshot: TokenEngineSnapshot, continuation_token_id: str
    ) -> str:
        join = next(
            item for item in snapshot.joins if item.continuation_token_id == continuation_token_id
        )
        inbound = {item.inbound_edge_id for item in join.obligations}
        if len(inbound) != 1:
            raise TokenRuntimeUnsupportedError(
                "nested continuation cannot represent multiple outer inbound routes"
            )
        return next(iter(inbound))

    @staticmethod
    def _reachable_inbound_edges(graph: Graph, source: str, target: str) -> list[str]:
        found: set[str] = set()
        pending = [source]
        visited: set[str] = set()
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            for edge in graph.edges:
                if not edge.enabled or edge.kind == "tool" or edge.source_node_id != current:
                    continue
                if edge.target_node_id == target:
                    found.add(edge.edge_id)
                elif edge.target_node_id not in visited:
                    pending.append(edge.target_node_id)
        return sorted(found)

    def _trace_resolution(self, run, edge, delivered, payload, token, *, tag=None) -> None:
        observer = self.driver.orchestrator
        if observer is None or not hasattr(observer, "_record_forward_resolution"):
            return
        saved = deepcopy(run.metadata)
        pending = list(run.pending_node_ids)
        if tag is None:
            tag = self._source_trace_tag(run, token.token_id)
        observer._record_forward_resolution(
            run, edge.target_node_id, edge.edge_id, delivered, payload, tag
        )
        run.metadata.clear()
        run.metadata.update(saved)
        run.pending_node_ids[:] = pending

    def _trace_join_ready(self, run, node_id, payload, token, *, tag=None) -> None:
        observer = self.driver.orchestrator
        if observer is None or not hasattr(observer, "_stash_join_payload"):
            return
        saved = deepcopy(run.metadata)
        pending = list(run.pending_node_ids)
        if tag is None:
            tag = self._source_trace_tag(run, token.token_id)
        observer._stash_join_payload(run, node_id, payload, tag)
        run.metadata.clear()
        run.metadata.update(saved)
        run.pending_node_ids[:] = pending

    @staticmethod
    def _source_trace_tag(run: Run, token_id: str) -> _ts.TokenTag:
        raw = run.metadata.get("token_trace_tags", {}).get(token_id, ())
        return tuple((str(header), int(index)) for header, index in raw)

    def _trace_suppressed_cascade(self, graph, run, node_id, token) -> None:
        back_edges = self.driver._back_edge_ids(graph)
        scopes = self.driver._graph_scopes(graph)
        pending = [node_id]
        visited: set[str] = set()
        while pending:
            source = pending.pop()
            if source in visited:
                continue
            visited.add(source)
            for edge in graph.edges:
                if (
                    edge.enabled
                    and edge.kind != "tool"
                    and edge.source_node_id == source
                    and edge.edge_id not in back_edges
                    and edge.edge_id not in scopes.exit_owner
                ):
                    self._trace_resolution(run, edge, False, None, token)
                    pending.append(edge.target_node_id)

    async def _complete_run(self, run: Run) -> Run:
        run.status = RunStatus.COMPLETED
        run.current_node_ids = []
        run.current_step = None
        run.final_output = run.metadata.get("last_output")
        run.metadata.pop("token_dispatch", None)
        run.metadata.pop("in_flight_dispatch", None)
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

    def _is_convergent(self, graph: Graph, node_id: str) -> bool:
        back_edges = self.driver._back_edge_ids(graph)
        return (
            sum(
                edge.enabled
                and edge.kind != "tool"
                and edge.edge_id not in back_edges
                and edge.target_node_id == node_id
                for edge in graph.edges
            )
            > 1
        )

    @staticmethod
    def _unreachable_inbound_sources(graph: Graph, target: str) -> list[str]:
        entry = graph.entry_step
        if entry is None:
            return []
        reachable = {entry}
        pending = [entry]
        while pending:
            current = pending.pop()
            for edge in graph.edges:
                if (
                    edge.enabled
                    and edge.kind != "tool"
                    and edge.source_node_id == current
                    and edge.target_node_id not in reachable
                ):
                    reachable.add(edge.target_node_id)
                    pending.append(edge.target_node_id)
        return sorted(
            edge.source_node_id
            for edge in graph.edges
            if edge.enabled
            and edge.kind != "tool"
            and edge.target_node_id == target
            and edge.source_node_id not in reachable
        )


__all__ = ["TokenRuntimeSupport", "TokenRuntimeUnsupportedError"]
