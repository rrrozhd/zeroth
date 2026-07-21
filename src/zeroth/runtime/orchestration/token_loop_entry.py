"""Deterministic entry transitions for token-owned loops."""

from __future__ import annotations

from collections.abc import Mapping

from zeroth.contracts.graph.token_snapshot import TokenEngineSnapshot
from zeroth.contracts.graph.tokens import (
    IterationFrame,
    IterationFrameState,
    IterationMember,
    IterationMembership,
    IterationMemberState,
    LoopEnclosingOwner,
    LoopExit,
    LoopInstance,
    LoopLifecycleState,
    SchedulingState,
    TokenLifecycleState,
)
from zeroth.runtime.orchestration.token_loop_helpers import (
    entry_fingerprint,
    frame_id,
    loop_id,
    loop_token_id,
    model_data,
    next_snapshot,
    replace_token,
    source_token,
    updated_token,
)
from zeroth.runtime.orchestration.token_loop_models import TokenLoopTransitionError


def _replace_loop(loops: list[LoopInstance], replacement: LoopInstance) -> None:
    for index, loop in enumerate(loops):
        if loop.loop_instance_id == replacement.loop_instance_id:
            loops[index] = replacement
            return
    raise TokenLoopTransitionError("loop replacement has no durable owner")


def _transfer_member_to_child(
    loop: LoopInstance, owner_token_id: str, child_token_id: str, revision: int
) -> LoopInstance:
    membership = next(
        (
            item
            for frame in loop.frames
            for item in frame.members
            if item.token_id == owner_token_id and item.state is IterationMemberState.ACTIVE
        ),
        None,
    )
    if membership is None:
        raise TokenLoopTransitionError("nested loop owner has no active enclosing member")
    frames: list[IterationFrame] = []
    for frame in loop.frames:
        if owner_token_id not in {item.token_id for item in frame.members}:
            frames.append(frame)
            continue
        members = tuple(
            IterationMember(
                token_id=item.token_id,
                state=IterationMemberState.INTERNAL_COMPLETION,
                settled_revision=revision,
            )
            if item.token_id == owner_token_id
            else item
            for item in frame.members
        ) + (IterationMember(token_id=child_token_id, state=IterationMemberState.ACTIVE),)
        frames.append(
            IterationFrame.model_validate(
                {**model_data(frame), "members": members, "updated_revision": revision}
            )
        )
    return LoopInstance.model_validate(
        {
            **model_data(loop),
            "frames": tuple(frames),
            "live_child_token_ids": tuple(
                sorted(
                    item.token_id
                    for frame in frames
                    for item in frame.members
                    if item.state is IterationMemberState.ACTIVE
                )
            ),
            "next_token_ordinal": loop.next_token_ordinal + 1,
            "updated_revision": revision,
        }
    )


def enter_loop(
    snapshot: TokenEngineSnapshot,
    *,
    token_id: str,
    dispatch_id: str | None = None,
    attempt: int | None = None,
    cancellation_generation: int | None = None,
    loop_header_node_id: str,
    body_node_id: str,
    inbound_edge_id: str,
    exit_routes: Mapping[str, str],
) -> TokenEngineSnapshot:
    """Retire a queued header token and atomically create iteration zero."""
    if not exit_routes or any(not edge or not target for edge, target in exit_routes.items()):
        raise TokenLoopTransitionError("loop entry requires non-empty exit edge/target routes")
    fingerprint = entry_fingerprint(
        token_id=token_id,
        loop_header_node_id=loop_header_node_id,
        body_node_id=body_node_id,
        inbound_edge_id=inbound_edge_id,
        exit_routes=exit_routes,
        dispatch_id=dispatch_id,
        attempt=attempt,
        cancellation_generation=cancellation_generation,
    )
    persisted = [loop for loop in snapshot.loops if loop.enclosing_owner.token_id == token_id]
    if persisted:
        if len(persisted) == 1 and persisted[0].entry_command_fingerprint == fingerprint:
            return snapshot
        raise TokenLoopTransitionError("loop entry replay contradicts persisted command")
    owner = source_token(
        snapshot,
        token_id,
        dispatch_id=dispatch_id,
        attempt=attempt,
        cancellation_generation=cancellation_generation,
    )
    if owner.current_node_id != loop_header_node_id:
        raise TokenLoopTransitionError("loop entry header contradicts the source token")
    revision = snapshot.revision + 1
    instance_id = loop_id(snapshot, owner, loop_header_node_id)
    child_id = loop_token_id(snapshot, instance_id, 0)
    current_frame_id = frame_id(snapshot, instance_id, 0)
    membership = IterationMembership(
        loop_instance_id=instance_id,
        parent_loop_instance_id=(
            owner.iteration_memberships[-1].loop_instance_id
            if owner.iteration_memberships
            else None
        ),
        iteration_frame_id=current_frame_id,
        loop_header_node_id=loop_header_node_id,
        iteration_index=0,
    )
    child = updated_token(
        owner,
        token_id=child_id,
        parent_token_id=owner.token_id,
        current_node_id=body_node_id,
        causal_inbound_edge_id=inbound_edge_id,
        provenance_tag=tuple(
            sorted(
                (
                    *owner.provenance_tag,
                    {
                        "loop_header_node_id": loop_header_node_id,
                        "iteration_index": 0,
                    },
                ),
                key=lambda item: (
                    item.loop_header_node_id
                    if hasattr(item, "loop_header_node_id")
                    else item["loop_header_node_id"]
                ),
            )
        ),
        scheduling_state=SchedulingState.QUEUED,
        lifecycle_state=TokenLifecycleState.ACTIVE,
        iteration_memberships=(*owner.iteration_memberships, membership),
        state_revision=revision,
        settled_revision=None,
    )
    settled_owner = updated_token(
        owner,
        scheduling_state=SchedulingState.SETTLED,
        lifecycle_state=TokenLifecycleState.SETTLED,
        state_revision=revision,
        settled_revision=revision,
    )
    frame = IterationFrame(
        iteration_frame_id=current_frame_id,
        loop_instance_id=instance_id,
        iteration_index=0,
        members=(IterationMember(token_id=child_id, state=IterationMemberState.ACTIVE),),
        state=IterationFrameState.ACTIVE,
        created_revision=revision,
        updated_revision=revision,
    )
    enclosing = LoopEnclosingOwner(
        token_id=owner.token_id,
        enclosing_loop_instance_id=(
            owner.iteration_memberships[-1].loop_instance_id
            if owner.iteration_memberships
            else None
        ),
        iteration_frame_id=(
            owner.iteration_memberships[-1].iteration_frame_id
            if owner.iteration_memberships
            else None
        ),
    )
    loop = LoopInstance(
        loop_instance_id=instance_id,
        loop_header_node_id=loop_header_node_id,
        entry_command_fingerprint=fingerprint,
        enclosing_owner=enclosing,
        outer_provenance_tag=owner.provenance_tag,
        frames=(frame,),
        live_child_token_ids=(child_id,),
        next_token_ordinal=1,
        exits=tuple(
            LoopExit(exit_edge_id=edge, target_node_id=target)
            for edge, target in sorted(exit_routes.items())
        ),
        lifecycle_state=LoopLifecycleState.RUNNING,
        created_revision=revision,
        updated_revision=revision,
    )
    loops = list(snapshot.loops)
    if owner.iteration_memberships:
        outer_id = owner.iteration_memberships[-1].loop_instance_id
        outer = next((item for item in loops if item.loop_instance_id == outer_id), None)
        if outer is None:
            raise TokenLoopTransitionError("nested loop owner has a missing outer instance")
        _replace_loop(loops, _transfer_member_to_child(outer, owner.token_id, child_id, revision))
    loops.append(loop)
    return next_snapshot(
        snapshot,
        next_token_ordinal=snapshot.next_token_ordinal + 1,
        queue=tuple(item for item in snapshot.queue if item.token_id != token_id) + (child,),
        tokens=(*replace_token(snapshot.tokens, settled_owner), child),
        loops=tuple(loops),
        in_flight_dispatches=tuple(
            item for item in snapshot.in_flight_dispatches if item.dispatch_id != dispatch_id
        ),
    )


__all__ = ["enter_loop"]
