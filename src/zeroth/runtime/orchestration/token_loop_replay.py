"""Exact replay validation for durable loop-member settlements."""

from __future__ import annotations

from pydantic import JsonValue

from zeroth.contracts.graph.token_snapshot import TokenEngineSnapshot
from zeroth.contracts.graph.tokens import IterationMemberState, SchedulingState
from zeroth.runtime.orchestration.token_loop_models import TokenLoopTransitionError


def _settlement_replay(
    snapshot: TokenEngineSnapshot,
    *,
    token_id: str,
    outcome: IterationMemberState,
    edge_id: str | None,
    target_node_id: str | None,
    payload: JsonValue,
    crossed_loop_instance_ids: tuple[str, ...] | None,
    command_fingerprint: str,
) -> TokenEngineSnapshot | None:
    token = next((item for item in snapshot.tokens if item.token_id == token_id), None)
    if token is None or token.scheduling_state is not SchedulingState.SETTLED:
        return None
    membership_ids = tuple(item.loop_instance_id for item in token.iteration_memberships)
    crossed = crossed_loop_instance_ids or (membership_ids[-1],)
    if tuple(crossed) != membership_ids[len(membership_ids) - len(crossed) :]:
        raise TokenLoopTransitionError("settlement replay crosses a different loop suffix")
    outermost = crossed[0]
    for loop_id in crossed:
        loop = next((item for item in snapshot.loops if item.loop_instance_id == loop_id), None)
        membership = next(
            item for item in token.iteration_memberships if item.loop_instance_id == loop_id
        )
        frame = (
            None
            if loop is None
            else next(
                (
                    item
                    for item in loop.frames
                    if item.iteration_frame_id == membership.iteration_frame_id
                ),
                None,
            )
        )
        member = (
            None
            if frame is None
            else next((item for item in frame.members if item.token_id == token_id), None)
        )
        expected = outcome
        if loop_id != outermost and outcome in {
            IterationMemberState.EXIT_DELIVERY,
            IterationMemberState.BACK_EDGE_CONTINUATION,
        }:
            expected = IterationMemberState.INTERNAL_COMPLETION
        if (
            member is None
            or member.state is not expected
            or member.settlement_command_fingerprint != command_fingerprint
        ):
            raise TokenLoopTransitionError("loop settlement replay contradicts persisted outcome")
    outer_loop = next(item for item in snapshot.loops if item.loop_instance_id == outermost)
    if outcome is IterationMemberState.BACK_EDGE_CONTINUATION:
        records = [
            item
            for frame in outer_loop.frames
            for item in frame.continuation_deliveries
            if item.token_id == token_id
        ]
        if (
            len(records) != 1
            or records[0].back_edge_id != edge_id
            or (records[0].delivery.model_dump(mode="json")["payload"] != payload)
        ):
            raise TokenLoopTransitionError("loop settlement replay contradicts persisted delivery")
    elif (
        outcome in {IterationMemberState.EXIT_DELIVERY, IterationMemberState.SUPPRESSED}
        and edge_id is not None
    ):
        records = [
            record
            for exit_state in outer_loop.exits
            for record in exit_state.records
            if record.token_id == token_id
        ]
        expected_payload = payload if outcome is IterationMemberState.EXIT_DELIVERY else None
        actual_payload = (
            None
            if len(records) != 1 or records[0].delivery is None
            else records[0].delivery.model_dump(mode="json")["payload"]
        )
        if (
            len(records) != 1
            or records[0].exit_edge_id != edge_id
            or records[0].target_node_id != target_node_id
            or actual_payload != expected_payload
        ):
            raise TokenLoopTransitionError("loop settlement replay contradicts persisted delivery")
    return snapshot


__all__: list[str] = []
