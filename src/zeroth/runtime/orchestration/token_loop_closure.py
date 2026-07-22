"""Iteration advancement, loop finalization, and CAS reducer ownership."""

from __future__ import annotations

from typing import cast

from pydantic import JsonValue

from zeroth.contracts.graph.models import JoinConfig
from zeroth.contracts.graph.token_snapshot import TokenEngineSnapshot
from zeroth.contracts.graph.tokens import (
    ForkLifecycleState,
    IterationFrame,
    IterationFrameState,
    IterationMember,
    IterationMembership,
    IterationMemberState,
    LoopExit,
    LoopExitResolutionOutcome,
    LoopInstance,
    LoopLifecycleState,
    SchedulingState,
    TokenEnvelope,
    TokenLifecycleState,
)
from zeroth.runtime.orchestration.token_join_models import JoinReducerInput
from zeroth.runtime.orchestration.token_join_reducers import reduce_join_inputs
from zeroth.runtime.orchestration.token_loop_forks import (
    _apply_exit_fork_ownership,
    _exit_lineage,
)
from zeroth.runtime.orchestration.token_loop_helpers import (
    common_fork_lineage,
    frame_id,
    loop_token_id,
    model_data,
    next_provenance,
    next_snapshot,
    replace_loop,
    replace_token,
    stable_fingerprint,
    updated_token,
)
from zeroth.runtime.orchestration.token_loop_models import (
    LoopReducer,
    LoopReductionClaim,
    LoopReductionClaimChangedError,
    TokenLoopTransitionError,
)


def _json_value(value: object) -> JsonValue:
    from zeroth.runtime.orchestration.token_join_reducers import _json_value

    return _json_value(value)


def _loop(snapshot: TokenEngineSnapshot, loop_instance_id: str) -> LoopInstance:
    loop = next(
        (item for item in snapshot.loops if item.loop_instance_id == loop_instance_id), None
    )
    if loop is None:
        raise TokenLoopTransitionError(f"loop {loop_instance_id!r} does not exist")
    return loop


def _inputs(snapshot: TokenEngineSnapshot, loop: LoopInstance) -> tuple[JoinReducerInput, ...]:
    current = loop.frames[-1]
    return tuple(
        JoinReducerInput(
            source_token_id=item.token_id,
            inbound_edge_id=item.back_edge_id,
            payload=item.delivery.model_dump(mode="json")["payload"],
            order=item.canonical_order,
        )
        for item in current.continuation_deliveries
    )


def _config_fingerprint(config: JoinConfig) -> str:
    return stable_fingerprint(config.model_dump(mode="json"))


def _resumed_fork_lineage(snapshot: TokenEngineSnapshot, tokens: tuple[TokenEnvelope, ...]):
    lineage = list(common_fork_lineage(tokens))
    forks = {item.fork_id: item for item in snapshot.forks}
    while lineage and forks[lineage[-1].fork_id].lifecycle_state is ForkLifecycleState.CLOSED:
        lineage.pop()
    return tuple(lineage)


def _claim_matches(loop: LoopInstance, claim: LoopReductionClaim) -> bool:
    return (
        loop.reduction_claim_id == claim.claim_id
        and loop.reduction_claim_owner_id == claim.owner_id
        and loop.reduction_attempt == claim.attempt
        and loop.reduction_claim_revision == claim.claimed_revision
    )


def _strip_compacted_memberships(
    tokens: tuple[TokenEnvelope, ...],
    compacted_frame_ids: set[str],
    closed_fork_children: dict[str, set[str]],
) -> tuple[TokenEnvelope, ...]:
    updated: list[TokenEnvelope] = []
    for token in tokens:
        removed_headers = {
            item.loop_header_node_id
            for item in token.iteration_memberships
            if item.iteration_frame_id in compacted_frame_ids
        }
        if not removed_headers:
            updated.append(token)
            continue
        data = model_data(token)
        data["iteration_memberships"] = tuple(
            item
            for item in token.iteration_memberships
            if item.iteration_frame_id not in compacted_frame_ids
        )
        lineage = list(token.fork_lineage)
        while (
            lineage
            and lineage[-1].fork_id in closed_fork_children
            and token.token_id not in closed_fork_children[lineage[-1].fork_id]
        ):
            lineage.pop()
        data["fork_lineage"] = tuple(lineage)
        data["provenance_tag"] = tuple(
            item for item in token.provenance_tag if item.loop_header_node_id not in removed_headers
        )
        updated.append(TokenEnvelope.model_validate(data))
    return tuple(updated)


def _compact_join_memberships(snapshot: TokenEngineSnapshot, compacted_frame_ids: set[str]):
    joins = []
    for join in snapshot.joins:
        removed_headers = {
            membership.loop_header_node_id
            for membership in join.iteration_memberships
            if membership.iteration_frame_id in compacted_frame_ids
        }
        if not removed_headers:
            joins.append(join)
            continue
        data = model_data(join)
        data["iteration_memberships"] = tuple(
            membership
            for membership in join.iteration_memberships
            if membership.iteration_frame_id not in compacted_frame_ids
        )
        data["provenance_tag"] = tuple(
            frame
            for frame in join.provenance_tag
            if frame.loop_header_node_id not in removed_headers
        )
        joins.append(type(join).model_validate(data))
    return tuple(joins)


def _transfer_exit_join_ownership(
    snapshot: TokenEngineSnapshot,
    continuations: tuple[TokenEnvelope, ...],
    revision: int,
):
    replacements: dict[str, str] = {}
    forks = {fork.fork_id: fork for fork in snapshot.forks}
    for continuation in continuations:
        if not continuation.fork_lineage:
            continue
        frame = next(
            (item for item in reversed(continuation.fork_lineage) if item.fork_id in forks),
            None,
        )
        if frame is None:
            continue
        fork = forks[frame.fork_id]
        source = next(
            child.token_id
            for child in fork.children
            if child.creation_ordinal == frame.child_ordinal
        )
        replacements[source] = continuation.token_id
    if not replacements:
        return snapshot.joins, snapshot.tokens

    joins = []
    for join in snapshot.joins:
        obligations = []
        changed = False
        for obligation in join.obligations:
            replacement = replacements.get(obligation.source_token_id)
            if replacement is None or obligation.outcome is not None:
                obligations.append(obligation)
                continue
            changed = True
            obligations.append(
                type(obligation).model_validate(
                    {**model_data(obligation), "source_token_id": replacement}
                )
            )
        joins.append(
            type(join).model_validate({**model_data(join), "obligations": tuple(obligations)})
            if changed
            else join
        )

    tokens = snapshot.tokens
    for source_id in replacements:
        source = next(token for token in tokens if token.token_id == source_id)
        if source.scheduling_state is not SchedulingState.JOIN_WAITING:
            continue
        tokens = replace_token(
            tokens,
            updated_token(
                source,
                scheduling_state=SchedulingState.SETTLED,
                lifecycle_state=TokenLifecycleState.SETTLED,
                state_revision=revision,
                settled_revision=revision,
            ),
        )
    return tuple(joins), tokens


def _transfer_to_outer(
    snapshot: TokenEngineSnapshot,
    inner: LoopInstance,
    continuations: tuple[TokenEnvelope, ...],
    revision: int,
) -> tuple[LoopInstance, ...]:
    outer_id = inner.enclosing_owner.enclosing_loop_instance_id
    if outer_id is None:
        return snapshot.loops
    outer = _loop(snapshot, outer_id)
    owned_ids = {member.token_id for frame in inner.frames for member in frame.members} | {
        inner.enclosing_owner.token_id
    }
    settlement_fingerprints = {
        member.settlement_command_fingerprint
        for frame in inner.frames
        for member in frame.members
        if member.settlement_command_fingerprint is not None
    }
    owner_frame = next(
        (
            frame
            for frame in outer.frames
            if frame.iteration_frame_id == inner.enclosing_owner.iteration_frame_id
        ),
        None,
    )
    if owner_frame is None:
        raise TokenLoopTransitionError("completed nested loop has no outer owner frame")
    active_owned = tuple(
        member
        for member in owner_frame.members
        if member.token_id in owned_ids and member.state is IterationMemberState.ACTIVE
    )
    if not active_owned:
        transferred = tuple(
            member
            for member in owner_frame.members
            if member.token_id in owned_ids
            and member.settlement_command_fingerprint in settlement_fingerprints
        )
        if continuations or not transferred:
            raise TokenLoopTransitionError(
                "completed nested loop contradicts persisted outer owner settlement"
            )
        return snapshot.loops
    frames: list[IterationFrame] = []
    matched = False
    for frame in outer.frames:
        if frame.iteration_frame_id != inner.enclosing_owner.iteration_frame_id:
            frames.append(frame)
            continue
        members: list[IterationMember] = []
        for member in frame.members:
            if member.token_id in owned_ids and member.state is IterationMemberState.ACTIVE:
                matched = True
                members.append(
                    IterationMember(
                        token_id=member.token_id,
                        state=IterationMemberState.INTERNAL_COMPLETION,
                        settled_revision=revision,
                    )
                )
            else:
                members.append(member)
        members.extend(
            IterationMember(token_id=item.token_id, state=IterationMemberState.ACTIVE)
            for item in continuations
        )
        state = (
            IterationFrameState.ACTIVE
            if any(item.state is IterationMemberState.ACTIVE for item in members)
            else IterationFrameState.BARRIER_READY
        )
        frames.append(
            IterationFrame.model_validate(
                {
                    **model_data(frame),
                    "members": tuple(members),
                    "state": state,
                    "updated_revision": revision,
                }
            )
        )
    if not matched:
        raise TokenLoopTransitionError("completed nested loop has no active outer owner")
    replacement = LoopInstance.model_validate(
        {
            **model_data(outer),
            "frames": tuple(frames),
            "live_child_token_ids": tuple(
                sorted(
                    item.token_id
                    for frame in frames
                    for item in frame.members
                    if item.state is IterationMemberState.ACTIVE
                )
            ),
            "next_token_ordinal": outer.next_token_ordinal + len(continuations),
            "updated_revision": revision,
        }
    )
    return replace_loop(snapshot.loops, replacement)


def close_ready_loop(
    snapshot: TokenEngineSnapshot,
    loop_instance_id: str,
    *,
    continuation_config: JoinConfig | None = None,
    reducer: LoopReducer = reduce_join_inputs,
    claimed_reduction: LoopReductionClaim | None = None,
    deferred_exit_edge_ids: frozenset[str] = frozenset(),
) -> TokenEngineSnapshot:
    """Advance one barrier-ready frame or finalize its loop exactly once."""
    loop = _loop(snapshot, loop_instance_id)
    if loop.lifecycle_state is LoopLifecycleState.COMPLETED:
        return snapshot
    current = loop.frames[-1]
    if current.state is IterationFrameState.ACTIVE:
        if len(loop.frames) > 1 and loop.frames[-2].state is IterationFrameState.SETTLED:
            return snapshot
        raise TokenLoopTransitionError("loop iteration barrier is not ready")
    if current.state is IterationFrameState.SETTLED:
        return snapshot
    if loop.reduction_claim_id is not None:
        if claimed_reduction is None or not _claim_matches(loop, claimed_reduction):
            raise LoopReductionClaimChangedError("loop reduction claim changed")
    elif claimed_reduction is not None:
        raise LoopReductionClaimChangedError("loop has no matching reduction claim")

    revision = snapshot.revision + 1
    continuation_inputs = _inputs(snapshot, loop)
    tokens_by_id = {item.token_id: item for item in snapshot.tokens}
    if continuation_inputs:
        if len(continuation_inputs) > 1 and continuation_config is None:
            raise TokenLoopTransitionError(
                "multiple back-edge deliveries require destination header JoinConfig"
            )
        if continuation_config is None:
            reduced = continuation_inputs[0].payload
        else:
            if loop.reducer_fingerprint is not None and (
                loop.reducer_fingerprint != _config_fingerprint(continuation_config)
            ):
                raise TokenLoopTransitionError("claimed reducer configuration changed")
            reduced = _json_value(reducer(continuation_config, continuation_inputs))
        next_index = current.iteration_index + 1
        token_id = loop_token_id(snapshot, loop.loop_instance_id, loop.next_token_ordinal)
        next_frame_id = frame_id(snapshot, loop.loop_instance_id, next_index)
        owner = tokens_by_id[loop.enclosing_owner.token_id]
        membership = IterationMembership(
            loop_instance_id=loop.loop_instance_id,
            parent_loop_instance_id=loop.enclosing_owner.enclosing_loop_instance_id,
            iteration_frame_id=next_frame_id,
            loop_header_node_id=loop.loop_header_node_id,
            iteration_index=next_index,
        )
        source_tokens = tuple(tokens_by_id[item.source_token_id] for item in continuation_inputs)
        continuation = TokenEnvelope(
            token_id=token_id,
            continuation_parent_token_ids=tuple(
                item.source_token_id for item in continuation_inputs
            ),
            provenance_tag=next_provenance(loop, next_index),
            current_node_id=loop.loop_header_node_id,
            causal_inbound_edge_id=(
                continuation_inputs[0].inbound_edge_id
                if len({item.inbound_edge_id for item in continuation_inputs}) == 1
                else None
            ),
            payload=reduced,
            lifecycle_state=TokenLifecycleState.ACTIVE,
            scheduling_state=SchedulingState.QUEUED,
            fork_lineage=_resumed_fork_lineage(snapshot, source_tokens),
            iteration_memberships=(*owner.iteration_memberships, membership),
            cancellation_generation=source_tokens[0].cancellation_generation,
            state_revision=revision,
        )
        settled_frame = IterationFrame.model_validate(
            {
                **model_data(current),
                "state": IterationFrameState.SETTLED,
                "updated_revision": revision,
                "settled_revision": revision,
            }
        )
        next_frame = IterationFrame(
            iteration_frame_id=next_frame_id,
            loop_instance_id=loop.loop_instance_id,
            iteration_index=next_index,
            members=(IterationMember(token_id=token_id, state=IterationMemberState.ACTIVE),),
            state=IterationFrameState.ACTIVE,
            created_revision=revision,
            updated_revision=revision,
        )
        retained_frames = (*loop.frames[:-1], settled_frame, next_frame)
        compacted = retained_frames[:-2]
        retained_frames = retained_frames[-2:]
        compacted_ids = {frame.iteration_frame_id for frame in compacted}
        tokens = _strip_compacted_memberships(
            snapshot.tokens,
            compacted_ids,
            {
                fork.fork_id: {child.token_id for child in fork.children}
                for fork in snapshot.forks
                if fork.lifecycle_state is ForkLifecycleState.CLOSED
            },
        )
        replacement = LoopInstance.model_validate(
            {
                **model_data(loop),
                "frames": retained_frames,
                "live_child_token_ids": (token_id,),
                "next_token_ordinal": loop.next_token_ordinal + 1,
                "reducer_fingerprint": None,
                "reduction_claim_id": None,
                "reduction_claim_owner_id": None,
                "reduction_claim_revision": None,
                "updated_revision": revision,
            }
        )
        loops = replace_loop(snapshot.loops, replacement)
        if loop.enclosing_owner.enclosing_loop_instance_id is not None:
            intermediate = TokenEngineSnapshot.model_construct(
                **{**model_data(snapshot), "loops": loops}
            )
            loops = _transfer_to_outer(intermediate, replacement, (continuation,), revision)
            loops = replace_loop(loops, replacement)
        return next_snapshot(
            snapshot,
            next_token_ordinal=snapshot.next_token_ordinal + 1,
            queue=(*snapshot.queue, continuation),
            tokens=(*tokens, continuation),
            joins=_compact_join_memberships(snapshot, compacted_ids),
            loops=loops,
        )

    resolved_exits: list[LoopExit] = []
    continuations: list[TokenEnvelope] = []
    allocation = loop.next_token_ordinal
    owner = tokens_by_id[loop.enclosing_owner.token_id]
    for exit_state in loop.exits:
        delivered_records = tuple(
            item
            for item in exit_state.records
            if item.outcome is LoopExitResolutionOutcome.DELIVERED
        )
        resolution = (
            LoopExitResolutionOutcome.DELIVERED
            if delivered_records
            else LoopExitResolutionOutcome.SUPPRESSED
        )
        resolved_exits.append(
            LoopExit.model_validate(
                {
                    **model_data(exit_state),
                    "resolution_outcome": resolution,
                    "resolved_revision": revision,
                }
            )
        )
        if not delivered_records:
            continue
        continuation_id = loop_token_id(snapshot, loop.loop_instance_id, allocation)
        allocation += 1
        parents = tuple(item.token_id for item in delivered_records)
        source_tokens = tuple(tokens_by_id[item] for item in parents)
        lineage = _exit_lineage(exit_state)
        deferred = exit_state.exit_edge_id in deferred_exit_edge_ids
        continuations.append(
            TokenEnvelope(
                token_id=continuation_id,
                continuation_parent_token_ids=parents,
                provenance_tag=loop.outer_provenance_tag,
                current_node_id=exit_state.target_node_id,
                causal_inbound_edge_id=exit_state.exit_edge_id,
                payload=cast(
                    JsonValue,
                    [
                        item.delivery.model_dump(mode="json")["payload"]
                        for item in delivered_records
                    ],
                ),
                lifecycle_state=(
                    TokenLifecycleState.SETTLED if deferred else TokenLifecycleState.ACTIVE
                ),
                scheduling_state=(SchedulingState.SETTLED if deferred else SchedulingState.QUEUED),
                fork_lineage=lineage,
                iteration_memberships=owner.iteration_memberships,
                cancellation_generation=source_tokens[0].cancellation_generation,
                state_revision=revision,
                settled_revision=revision if deferred else None,
            )
        )
    owned_continuations, forks = _apply_exit_fork_ownership(
        snapshot,
        loop,
        tuple(continuations),
        revision,
    )
    continuations = list(owned_continuations)
    joins, tokens = _transfer_exit_join_ownership(snapshot, tuple(continuations), revision)
    settled_frame = IterationFrame.model_validate(
        {
            **model_data(current),
            "state": IterationFrameState.SETTLED,
            "updated_revision": revision,
            "settled_revision": revision,
        }
    )
    replacement = LoopInstance.model_validate(
        {
            **model_data(loop),
            "frames": (*loop.frames[:-1], settled_frame),
            "live_child_token_ids": (),
            "next_token_ordinal": allocation,
            "exits": tuple(resolved_exits),
            "emitted_continuation_token_ids": tuple(
                sorted(item.token_id for item in continuations)
            ),
            "reducer_fingerprint": None,
            "reduction_claim_id": None,
            "reduction_claim_owner_id": None,
            "reduction_claim_revision": None,
            "lifecycle_state": LoopLifecycleState.COMPLETED,
            "updated_revision": revision,
            "completed_revision": revision,
        }
    )
    loops = replace_loop(snapshot.loops, replacement)
    if loop.enclosing_owner.enclosing_loop_instance_id is not None:
        intermediate = TokenEngineSnapshot.model_construct(
            **{**model_data(snapshot), "loops": loops}
        )
        loops = _transfer_to_outer(intermediate, replacement, tuple(continuations), revision)
        loops = replace_loop(loops, replacement)
    return next_snapshot(
        snapshot,
        next_token_ordinal=snapshot.next_token_ordinal + len(continuations),
        queue=(
            *snapshot.queue,
            *(item for item in continuations if item.scheduling_state is SchedulingState.QUEUED),
        ),
        tokens=(*tokens, *continuations),
        forks=forks,
        joins=joins,
        loops=loops,
    )


__all__ = ["close_ready_loop"]
