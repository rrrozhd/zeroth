"""Atomic member settlement across token-owned loop scopes."""

from __future__ import annotations

from pydantic import JsonValue

from zeroth.contracts.graph.token_snapshot import TokenEngineSnapshot
from zeroth.contracts.graph.tokens import (
    ForkInstance,
    ForkLifecycleState,
    ForkObligation,
    ForkObligationOutcome,
    IterationContinuationDelivery,
    IterationFrame,
    IterationFrameState,
    IterationMember,
    IterationMemberState,
    LoopExit,
    LoopExitRecord,
    LoopExitResolutionOutcome,
    LoopInstance,
    SchedulingState,
    TokenLifecycleState,
)
from zeroth.runtime.orchestration.token_loop_helpers import (
    canonical_order,
    delivery,
    model_data,
    next_snapshot,
    replace_token,
    source_token,
    stable_fingerprint,
    updated_token,
)
from zeroth.runtime.orchestration.token_loop_models import FailureMode, TokenLoopTransitionError
from zeroth.runtime.orchestration.token_loop_replay import _settlement_replay
from zeroth.runtime.orchestration.token_scheduler import (
    TokenSchedulerTransitionError,
    _settle_innermost_fork,
)


def _replace_loop(loops: list[LoopInstance], replacement: LoopInstance) -> None:
    for index, loop in enumerate(loops):
        if loop.loop_instance_id == replacement.loop_instance_id:
            loops[index] = replacement
            return
    raise TokenLoopTransitionError("loop replacement has no durable owner")


def _settle_fork(
    snapshot: TokenEngineSnapshot,
    token_id: str,
    *,
    outcome: IterationMemberState,
    edge_id: str | None,
    revision: int,
) -> tuple[ForkInstance, ...]:
    token = next(item for item in snapshot.tokens if item.token_id == token_id)
    if not token.fork_lineage:
        return snapshot.forks
    owner_id = token.fork_lineage[-1].fork_id
    fork_owner = next((item for item in snapshot.forks if item.fork_id == owner_id), None)
    if fork_owner is None:
        raise TokenLoopTransitionError("loop member has a missing fork owner")
    candidate_id: str | None = token_id
    tokens_by_id = {item.token_id: item for item in snapshot.tokens}
    fork_child_ids = {item.token_id for item in fork_owner.children}
    while candidate_id is not None and candidate_id not in fork_child_ids:
        candidate = tokens_by_id.get(candidate_id)
        candidate_id = None if candidate is None else candidate.parent_token_id
    if candidate_id is None:
        raise TokenLoopTransitionError("loop member has no ancestor fork obligation")
    mapped = {
        IterationMemberState.FAILED: ForkObligationOutcome.FAILED,
        IterationMemberState.CANCELLED: ForkObligationOutcome.CANCELLED,
        IterationMemberState.EXIT_DELIVERY: ForkObligationOutcome.EXITED,
    }.get(outcome, ForkObligationOutcome.SUPPRESSED)
    if mapped is not ForkObligationOutcome.EXITED:
        candidate = tokens_by_id.get(candidate_id)
        if candidate is None:
            raise TokenLoopTransitionError("fork obligation token is missing")
        try:
            return _settle_innermost_fork(
                snapshot,
                candidate,
                outcome=mapped,
                revision=revision,
            )
        except TokenSchedulerTransitionError as exc:
            raise TokenLoopTransitionError(str(exc)) from exc
    replacement: ForkInstance | None = None
    for fork in snapshot.forks:
        if fork.fork_id != owner_id:
            continue
        obligations: list[ForkObligation] = []
        for obligation in fork.obligations:
            if obligation.child_token_id != candidate_id:
                obligations.append(obligation)
                continue
            if obligation.outcome is not None:
                raise TokenLoopTransitionError("fork obligation is already settled")
            obligations.append(
                ForkObligation.model_validate(
                    {
                        **model_data(obligation),
                        "outcome": mapped,
                        "exit_edge_id": edge_id if mapped is ForkObligationOutcome.EXITED else None,
                        "settled_revision": revision,
                    }
                )
            )
        outstanding = sum(item.outcome is None for item in obligations)
        replacement = ForkInstance.model_validate(
            {
                **model_data(fork),
                "obligations": tuple(obligations),
                "outstanding_child_count": outstanding,
                "lifecycle_state": (
                    ForkLifecycleState.OPEN if outstanding else ForkLifecycleState.CLOSED
                ),
                "updated_revision": revision,
                "closed_revision": None if outstanding else revision,
            }
        )
    if replacement is None:
        raise TokenLoopTransitionError("loop member has a missing fork owner")
    return tuple(replacement if item.fork_id == owner_id else item for item in snapshot.forks)


def settle_loop_member(
    snapshot: TokenEngineSnapshot,
    *,
    token_id: str,
    dispatch_id: str | None = None,
    attempt: int | None = None,
    cancellation_generation: int | None = None,
    outcome: IterationMemberState,
    edge_id: str | None = None,
    target_node_id: str | None = None,
    payload: JsonValue = None,
    crossed_loop_instance_ids: tuple[str, ...] | None = None,
    failure_mode: FailureMode = "fail_fast",
    allow_failure_suppression: bool = False,
) -> TokenEngineSnapshot:
    """Retire one member and settle every explicitly crossed loop membership."""
    reported_outcome = outcome
    command_fingerprint = stable_fingerprint(
        {
            "token_id": token_id,
            "dispatch_id": dispatch_id,
            "attempt": attempt,
            "cancellation_generation": cancellation_generation,
            "outcome": reported_outcome.value,
            "edge_id": edge_id,
            "target_node_id": target_node_id,
            "payload": payload,
            "crossed_loop_instance_ids": crossed_loop_instance_ids,
            "failure_mode": failure_mode,
            "allow_failure_suppression": allow_failure_suppression,
        }
    )
    if outcome is IterationMemberState.ACTIVE:
        raise TokenLoopTransitionError("ACTIVE is not a settlement outcome")
    if outcome is IterationMemberState.FAILED and failure_mode == "best_effort":
        if not allow_failure_suppression:
            raise TokenLoopTransitionError("best-effort failure must be explicitly permitted")
        outcome = IterationMemberState.SUPPRESSED
    replayed = _settlement_replay(
        snapshot,
        token_id=token_id,
        outcome=outcome,
        edge_id=edge_id,
        target_node_id=target_node_id,
        payload=payload,
        crossed_loop_instance_ids=crossed_loop_instance_ids,
        command_fingerprint=command_fingerprint,
    )
    if replayed is not None:
        return replayed
    token = source_token(
        snapshot,
        token_id,
        dispatch_id=dispatch_id,
        attempt=attempt,
        cancellation_generation=cancellation_generation,
    )
    memberships = token.iteration_memberships
    if not memberships:
        raise TokenLoopTransitionError("token is not owned by an iteration frame")
    crossed = crossed_loop_instance_ids or (memberships[-1].loop_instance_id,)
    membership_ids = tuple(item.loop_instance_id for item in memberships)
    if tuple(crossed) != membership_ids[len(membership_ids) - len(crossed) :]:
        raise TokenLoopTransitionError("crossed loops must be one canonical innermost suffix")
    if (
        outcome
        in {
            IterationMemberState.BACK_EDGE_CONTINUATION,
            IterationMemberState.EXIT_DELIVERY,
        }
        and edge_id is None
    ):
        raise TokenLoopTransitionError("back-edge and exit deliveries require edge_id")
    if outcome is IterationMemberState.EXIT_DELIVERY and target_node_id is None:
        raise TokenLoopTransitionError("exit delivery requires target_node_id")
    revision = snapshot.revision + 1
    updated_loops: list[LoopInstance] = list(snapshot.loops)
    outermost_crossed = crossed[0]
    for loop_id_value in crossed:
        loop = next(
            (item for item in updated_loops if item.loop_instance_id == loop_id_value), None
        )
        if loop is None:
            raise TokenLoopTransitionError("crossed loop instance is missing")
        membership = next(item for item in memberships if item.loop_instance_id == loop_id_value)
        recorded_outcome = outcome
        if loop_id_value != outermost_crossed and outcome in {
            IterationMemberState.EXIT_DELIVERY,
            IterationMemberState.BACK_EDGE_CONTINUATION,
        }:
            recorded_outcome = IterationMemberState.INTERNAL_COMPLETION
        frames: list[IterationFrame] = []
        for frame in loop.frames:
            if frame.iteration_frame_id != membership.iteration_frame_id:
                frames.append(frame)
                continue
            members: list[IterationMember] = []
            matched = False
            for member in frame.members:
                if member.token_id != token_id:
                    members.append(member)
                    continue
                if member.state is not IterationMemberState.ACTIVE:
                    raise TokenLoopTransitionError("iteration member is already settled")
                matched = True
                members.append(
                    IterationMember(
                        token_id=token_id,
                        state=recorded_outcome,
                        causal_edge_id=(
                            edge_id
                            if recorded_outcome
                            in {
                                IterationMemberState.BACK_EDGE_CONTINUATION,
                                IterationMemberState.EXIT_DELIVERY,
                            }
                            or (
                                recorded_outcome is IterationMemberState.SUPPRESSED
                                and loop_id_value == outermost_crossed
                            )
                            else None
                        ),
                        settlement_command_fingerprint=command_fingerprint,
                        settled_revision=revision,
                    )
                )
            if not matched:
                raise TokenLoopTransitionError("token has no matching active frame member")
            continuations = frame.continuation_deliveries
            if recorded_outcome is IterationMemberState.BACK_EDGE_CONTINUATION:
                continuations = tuple(
                    sorted(
                        (
                            *continuations,
                            IterationContinuationDelivery(
                                token_id=token_id,
                                back_edge_id=edge_id or "",
                                delivery=delivery(payload),
                                canonical_order=canonical_order(token),
                                settled_revision=revision,
                            ),
                        ),
                        key=lambda item: item.canonical_order.sort_key(),
                    )
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
                        "continuation_deliveries": continuations,
                        "state": state,
                        "updated_revision": revision,
                    }
                )
            )
        exits = list(loop.exits)
        if (
            loop_id_value == outermost_crossed
            and outcome
            in {
                IterationMemberState.EXIT_DELIVERY,
                IterationMemberState.SUPPRESSED,
            }
            and edge_id is not None
        ):
            route_index = next(
                (index for index, item in enumerate(exits) if item.exit_edge_id == edge_id), None
            )
            if route_index is None or exits[route_index].target_node_id != target_node_id:
                raise TokenLoopTransitionError("loop exit route contradicts persisted ownership")
            exit_state = exits[route_index]
            record = LoopExitRecord(
                exit_edge_id=edge_id,
                target_node_id=target_node_id or "",
                token_id=token_id,
                outcome=(
                    LoopExitResolutionOutcome.DELIVERED
                    if outcome is IterationMemberState.EXIT_DELIVERY
                    else LoopExitResolutionOutcome.SUPPRESSED
                ),
                delivery=(
                    delivery(payload) if outcome is IterationMemberState.EXIT_DELIVERY else None
                ),
                canonical_order=canonical_order(token),
                settled_revision=revision,
            )
            exits[route_index] = LoopExit.model_validate(
                {
                    **model_data(exit_state),
                    "records": tuple(
                        sorted(
                            (*exit_state.records, record),
                            key=lambda item: item.canonical_order.sort_key(),
                        )
                    ),
                }
            )
        replacement = LoopInstance.model_validate(
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
                "exits": tuple(exits),
                "updated_revision": revision,
            }
        )
        _replace_loop(updated_loops, replacement)

    settled_ids = {token_id}
    if failure_mode == "fail_fast" and outcome in {
        IterationMemberState.FAILED,
        IterationMemberState.CANCELLED,
    }:
        owner = next(item for item in updated_loops if item.loop_instance_id == outermost_crossed)
        current = next(
            frame for frame in owner.frames if frame.state is not IterationFrameState.SETTLED
        )
        sibling_ids = {
            item.token_id for item in current.members if item.state is IterationMemberState.ACTIVE
        }
        settled_ids.update(sibling_ids)
        cancelled_members = tuple(
            IterationMember(
                token_id=item.token_id,
                state=IterationMemberState.CANCELLED,
                settlement_command_fingerprint=command_fingerprint,
                settled_revision=revision,
            )
            if item.token_id in sibling_ids
            else item
            for item in current.members
        )
        cancelled_frame = IterationFrame.model_validate(
            {
                **model_data(current),
                "members": cancelled_members,
                "state": IterationFrameState.BARRIER_READY,
                "updated_revision": revision,
            }
        )
        cancelled_owner = LoopInstance.model_validate(
            {
                **model_data(owner),
                "frames": tuple(
                    cancelled_frame
                    if frame.iteration_frame_id == current.iteration_frame_id
                    else frame
                    for frame in owner.frames
                ),
                "live_child_token_ids": (),
                "updated_revision": revision,
            }
        )
        _replace_loop(updated_loops, cancelled_owner)

    tokens = snapshot.tokens
    for settled_id in settled_ids:
        current_token = next(item for item in tokens if item.token_id == settled_id)
        tokens = replace_token(
            tokens,
            updated_token(
                current_token,
                scheduling_state=SchedulingState.SETTLED,
                lifecycle_state=TokenLifecycleState.SETTLED,
                state_revision=revision,
                settled_revision=revision,
            ),
        )
    forks = snapshot.forks
    settlements = ((token_id, outcome, edge_id),) + tuple(
        (settled_id, IterationMemberState.CANCELLED, None)
        for settled_id in sorted(settled_ids - {token_id})
    )
    for settled_id, fork_outcome, fork_edge_id in settlements:
        fork_snapshot = TokenEngineSnapshot.model_construct(
            **{**model_data(snapshot), "forks": forks}
        )
        forks = _settle_fork(
            fork_snapshot,
            settled_id,
            outcome=fork_outcome,
            edge_id=fork_edge_id,
            revision=revision,
        )
    return next_snapshot(
        snapshot,
        queue=tuple(item for item in snapshot.queue if item.token_id not in settled_ids),
        tokens=tokens,
        forks=forks,
        loops=tuple(updated_loops),
        in_flight_dispatches=tuple(
            item
            for item in snapshot.in_flight_dispatches
            if item.dispatch_id != dispatch_id and item.token.token_id not in settled_ids
        ),
    )


__all__ = ["settle_loop_member"]
