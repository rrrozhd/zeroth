"""Structured loop routing for the durable token runtime adapter."""

from __future__ import annotations

from functools import partial
from typing import Any, cast

from pydantic import JsonValue

from zeroth.contracts.graph import Graph
from zeroth.contracts.graph.tokens import (
    IterationMemberState,
    JoinObligationOutcome,
    LoopLifecycleState,
)
from zeroth.runtime.orchestration import token_scope as _ts
from zeroth.runtime.orchestration.token_joins import (
    deliver_to_join,
    settle_join_without_delivery,
)
from zeroth.runtime.orchestration.token_loops import (
    close_ready_loop,
    enter_loop,
    settle_loop_member,
)
from zeroth.runtime.orchestration.token_scheduler import DispatchClaim, FanOutBranch
from zeroth.runtime.orchestration.tool_executor import node_by_id


class TokenRuntimeLoopSupport:
    """Translate graph loop edges into authoritative snapshot transitions."""

    @staticmethod
    def _remember_loop_trace_tags(run: Any, snapshot: Any) -> None:
        trace_tags = dict(run.metadata.get("token_trace_tags", {}))
        for queued in snapshot.queue:
            trace_tags[queued.token_id] = [
                [frame.loop_header_node_id, frame.iteration_index]
                for frame in queued.provenance_tag
            ]
        run.metadata["token_trace_tags"] = trace_tags

    async def _route_loop_entry(
        self,
        graph: Graph,
        run: Any,
        claim: DispatchClaim,
        active: list[Any],
        output_data: dict[str, Any],
    ) -> tuple[bool, Any]:
        token = claim.dispatch.token
        header = token.current_node_id
        scopes = self.driver._graph_scopes(graph)
        body = scopes.bodies.get(header)
        if body is None or any(
            membership.loop_header_node_id == header for membership in token.iteration_memberships
        ):
            return False, claim.snapshot
        body_edges = [
            edge
            for edge in active
            if edge.edge_id not in scopes.back_edges and edge.target_node_id in body
        ]
        if not body_edges:
            # A pre-check header may bypass its loop without ever creating an
            # iteration owner.
            return False, claim.snapshot
        if len(body_edges) != len(active):
            from zeroth.runtime.orchestration.token_runtime_support import (
                TokenRuntimeUnsupportedError,
            )

            raise TokenRuntimeUnsupportedError(
                "a loop header cannot mix body-entry and exit deliveries"
            )
        edge = body_edges[0]
        branches = tuple(
            FanOutBranch(
                node_id=body_edge.target_node_id,
                inbound_edge_id=body_edge.edge_id,
                payload=cast(
                    JsonValue,
                    self.driver.edge_payload(
                        graph,
                        run,
                        header,
                        body_edge.target_node_id,
                        output_data,
                        body_edge,
                    ),
                ),
            )
            for body_edge in body_edges
        )
        payload = branches[0].payload
        exit_routes = {
            candidate.edge_id: candidate.target_node_id
            for candidate in graph.edges
            if candidate.enabled
            and candidate.kind != "tool"
            and candidate.edge_id in scopes.exit_edges[header]
        }
        dispatch = claim.dispatch
        committed = await self._transition(
            claim.snapshot,
            partial(
                enter_loop,
                token_id=token.token_id,
                dispatch_id=dispatch.dispatch_id,
                attempt=dispatch.attempt,
                cancellation_generation=dispatch.cancellation_generation,
                loop_header_node_id=header,
                body_node_id=edge.target_node_id,
                inbound_edge_id=edge.edge_id,
                exit_routes=exit_routes,
                body_payload=cast(JsonValue, payload),
                body_branches=branches if len(branches) > 1 else None,
            ),
        )
        source_tag = self._source_trace_tag(run, token.token_id)
        for body_edge, branch in zip(body_edges, branches, strict=True):
            target_tag = _ts.propagate_tag(source_tag, body_edge, scopes)
            self._trace_resolution(run, body_edge, True, branch.payload, token, tag=target_tag)
            self._trace_join_ready(
                run, body_edge.target_node_id, branch.payload, token, tag=target_tag
            )
        self._remember_loop_trace_tags(run, committed)
        return True, committed

    async def _route_loop_boundary(
        self,
        graph: Graph,
        run: Any,
        claim: DispatchClaim,
        active: list[Any],
        output_data: dict[str, Any],
    ) -> tuple[bool, Any]:
        token = claim.dispatch.token
        memberships = token.iteration_memberships
        if not memberships:
            return False, claim.snapshot
        scopes = self.driver._graph_scopes(graph)
        boundary = [
            edge
            for edge in active
            if edge.edge_id in scopes.back_edges or edge.edge_id in scopes.exit_owner
        ]
        internal = [edge for edge in active if edge not in boundary]
        if internal:
            return False, claim.snapshot
        if len(boundary) > 1:
            from zeroth.runtime.orchestration.token_runtime_support import (
                TokenRuntimeUnsupportedError,
            )

            raise TokenRuntimeUnsupportedError(
                "one loop member cannot publish multiple boundary deliveries"
            )

        edge = boundary[0] if boundary else None
        if edge is None:
            crossed = (memberships[-1].loop_instance_id,)
            outcome = IterationMemberState.INTERNAL_COMPLETION
            payload: JsonValue = None
            target_node_id = None
            edge_id = None
        else:
            owner_header = (
                edge.target_node_id
                if edge.edge_id in scopes.back_edges
                else scopes.exit_owner[edge.edge_id]
            )
            owner_index = next(
                index
                for index, membership in enumerate(memberships)
                if membership.loop_header_node_id == owner_header
            )
            crossed = tuple(membership.loop_instance_id for membership in memberships[owner_index:])
            outcome = (
                IterationMemberState.BACK_EDGE_CONTINUATION
                if edge.edge_id in scopes.back_edges
                else IterationMemberState.EXIT_DELIVERY
            )
            payload = cast(
                JsonValue,
                self.driver.edge_payload(
                    graph,
                    run,
                    token.current_node_id,
                    edge.target_node_id,
                    output_data,
                    edge,
                ),
            )
            target_node_id = edge.target_node_id
            edge_id = edge.edge_id

        outer_loop = next(
            loop for loop in claim.snapshot.loops if loop.loop_instance_id == crossed[0]
        )
        reserved_owner = next(
            token
            for token in claim.snapshot.tokens
            if token.token_id == outer_loop.enclosing_owner.token_id
        )
        reserved = next(
            (
                (join, obligation)
                for join in claim.snapshot.joins
                for obligation in join.obligations
                if obligation.source_token_id == reserved_owner.token_id
                and obligation.outcome is None
            ),
            None,
        )

        dispatch = claim.dispatch
        committed = await self._transition(
            claim.snapshot,
            partial(
                settle_loop_member,
                token_id=token.token_id,
                dispatch_id=dispatch.dispatch_id,
                attempt=dispatch.attempt,
                cancellation_generation=dispatch.cancellation_generation,
                outcome=outcome,
                edge_id=edge_id,
                target_node_id=target_node_id,
                payload=payload,
                crossed_loop_instance_ids=crossed,
                failure_mode=graph.execution_settings.failure_policy,
            ),
        )
        loops_by_id = {loop.loop_instance_id: loop for loop in committed.loops}
        for loop_id in reversed(crossed):
            loop = loops_by_id[loop_id]
            header = node_by_id(graph, loop.loop_header_node_id)
            committed = await self._transition(
                committed,
                partial(
                    close_ready_loop,
                    loop_instance_id=loop_id,
                    continuation_config=getattr(header, "join_config", None),
                    deferred_exit_edge_ids=(
                        frozenset({reserved[1].inbound_edge_id})
                        if reserved is not None and loop_id == outer_loop.loop_instance_id
                        else frozenset()
                    ),
                ),
            )
            loops_by_id = {item.loop_instance_id: item for item in committed.loops}
        outer_completed = (
            loops_by_id[outer_loop.loop_instance_id].lifecycle_state is LoopLifecycleState.COMPLETED
        )
        exit_traced = False
        if reserved is not None and outer_completed:
            join, obligation = reserved
            routes = {item.source_token_id: item.inbound_edge_id for item in join.obligations}
            reserved_edge = next(
                item for item in graph.edges if item.edge_id == obligation.inbound_edge_id
            )
            delivered = edge is not None and edge.edge_id == reserved_edge.edge_id
            transition = (
                partial(
                    deliver_to_join,
                    target_node_id=join.target_node_id,
                    inbound_edge_id=reserved_edge.edge_id,
                    cohort_inbound_edges=routes,
                    payload=payload,
                    token_id=reserved_owner.token_id,
                    failure_mode=graph.execution_settings.failure_policy,
                )
                if delivered
                else partial(
                    settle_join_without_delivery,
                    target_node_id=join.target_node_id,
                    inbound_edge_id=reserved_edge.edge_id,
                    cohort_inbound_edges=routes,
                    outcome=JoinObligationOutcome.SUPPRESSED,
                    token_id=reserved_owner.token_id,
                    failure_mode=graph.execution_settings.failure_policy,
                )
            )
            committed = await self._transition(committed, transition)
            source_tag = self._source_trace_tag(run, token.token_id)
            target_tag = _ts.propagate_tag(
                source_tag, reserved_edge, self.driver._graph_scopes(graph)
            )
            if delivered:
                self._trace_resolution(run, reserved_edge, True, payload, token, tag=target_tag)
                exit_traced = True
            else:
                self._trace_resolution(
                    run, reserved_edge, False, None, reserved_owner, tag=target_tag
                )
            committed = await self._close_join_if_ready(
                graph, run, committed, reserved_edge, target_tag
            )
        if edge is not None and outcome is IterationMemberState.EXIT_DELIVERY and not exit_traced:
            target_tag = _ts.propagate_tag(
                self._source_trace_tag(run, token.token_id),
                edge,
                self.driver._graph_scopes(graph),
            )
            self._trace_resolution(run, edge, True, payload, token, tag=target_tag)
        self._remember_loop_trace_tags(run, committed)
        return True, committed


__all__ = ["TokenRuntimeLoopSupport"]
