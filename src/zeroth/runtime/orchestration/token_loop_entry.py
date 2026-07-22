"""Deterministic entry transitions for token-owned loops."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import JsonValue

from zeroth.contracts.graph.token_snapshot import TokenEngineSnapshot
from zeroth.contracts.graph.tokens import (
    ForkChild,
    ForkInstance,
    ForkLifecycleState,
    ForkLineageFrame,
    ForkObligation,
    ForkObligationOutcome,
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
from zeroth.runtime.orchestration.token_scheduler import FanOutBranch, _stable_id


def _replace_loop(loops: list[LoopInstance], replacement: LoopInstance) -> None:
    for index, loop in enumerate(loops):
        if loop.loop_instance_id == replacement.loop_instance_id:
            loops[index] = replacement
            return
    raise TokenLoopTransitionError("loop replacement has no durable owner")


def _reserve_pending_join_owner(
    snapshot: TokenEngineSnapshot, owner_token_id: str, revision: int
) -> tuple[ForkInstance, ...]:
    """Keep an enclosing fork/join slot durable while its loop descendants run."""
    pending = tuple(
        (join, obligation)
        for join in snapshot.joins
        for obligation in join.obligations
        if obligation.source_token_id == owner_token_id and obligation.outcome is None
    )
    if not pending:
        return snapshot.forks
    if len(pending) != 1:
        raise TokenLoopTransitionError("loop owner has ambiguous pending join ownership")
    join, join_obligation = pending[0]
    forks: list[ForkInstance] = []
    matched = False
    for fork in snapshot.forks:
        if fork.fork_id != join.fork_id:
            forks.append(fork)
            continue
        obligations: list[ForkObligation] = []
        for obligation in fork.obligations:
            if (
                obligation.child_token_id != owner_token_id
                or obligation.child_ordinal != join_obligation.child_ordinal
            ):
                obligations.append(obligation)
                continue
            if obligation.outcome is not None:
                raise TokenLoopTransitionError("loop join owner fork slot is already settled")
            matched = True
            obligations.append(
                ForkObligation.model_validate(
                    {
                        **model_data(obligation),
                        "outcome": ForkObligationOutcome.JOINED,
                        "join_instance_id": join.join_instance_id,
                        "settled_revision": revision,
                    }
                )
            )
        outstanding = sum(item.outcome is None for item in obligations)
        forks.append(
            ForkInstance.model_validate(
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
        )
    if not matched:
        raise TokenLoopTransitionError("loop join owner has no matching fork slot")
    return tuple(forks)


def _transfer_member_to_children(
    loop: LoopInstance,
    owner_token_id: str,
    child_token_ids: tuple[str, ...],
    revision: int,
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
        ) + tuple(
            IterationMember(token_id=child_token_id, state=IterationMemberState.ACTIVE)
            for child_token_id in child_token_ids
        )
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
            "next_token_ordinal": loop.next_token_ordinal + len(child_token_ids),
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
    body_payload: JsonValue | None = None,
    body_branches: Sequence[FanOutBranch] | None = None,
) -> TokenEngineSnapshot:
    """Retire a queued header token and atomically create iteration zero."""
    if any(not edge or not target for edge, target in exit_routes.items()):
        raise TokenLoopTransitionError("loop entry routes require non-empty edge and target ids")
    fingerprint = entry_fingerprint(
        token_id=token_id,
        loop_header_node_id=loop_header_node_id,
        body_node_id=body_node_id,
        inbound_edge_id=inbound_edge_id,
        exit_routes=exit_routes,
        dispatch_id=dispatch_id,
        attempt=attempt,
        cancellation_generation=cancellation_generation,
        body_payload=body_payload,
        body_branches=body_branches,
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
    branches = tuple(body_branches or ()) or (
        FanOutBranch(
            node_id=body_node_id,
            inbound_edge_id=inbound_edge_id,
            payload=(
                owner.model_dump(mode="json")["payload"] if body_payload is None else body_payload
            ),
        ),
    )
    fork_id = (
        _stable_id("fork", snapshot.run_id, dispatch_id or owner.token_id, len(branches))
        if len(branches) > 1
        else None
    )
    children = tuple(
        updated_token(
            owner,
            token_id=loop_token_id(snapshot, instance_id, ordinal),
            parent_token_id=owner.token_id,
            continuation_parent_token_ids=(),
            current_node_id=branch.node_id,
            causal_inbound_edge_id=branch.inbound_edge_id,
            payload=branch.payload,
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
            fork_lineage=(
                owner.fork_lineage
                if fork_id is None
                else (
                    *owner.fork_lineage,
                    ForkLineageFrame(
                        fork_id=fork_id,
                        parent_fork_id=(
                            owner.fork_lineage[-1].fork_id if owner.fork_lineage else None
                        ),
                        child_ordinal=ordinal,
                    ),
                )
            ),
            scheduling_state=SchedulingState.QUEUED,
            lifecycle_state=TokenLifecycleState.ACTIVE,
            iteration_memberships=(*owner.iteration_memberships, membership),
            state_revision=revision,
            settled_revision=None,
        )
        for ordinal, branch in enumerate(branches)
    )
    owner_waits_for_join = any(
        obligation.source_token_id == owner.token_id and obligation.outcome is None
        for join in snapshot.joins
        for obligation in join.obligations
    )
    settled_owner = updated_token(
        owner,
        scheduling_state=(
            SchedulingState.JOIN_WAITING if owner_waits_for_join else SchedulingState.SETTLED
        ),
        lifecycle_state=(
            TokenLifecycleState.ACTIVE if owner_waits_for_join else TokenLifecycleState.SETTLED
        ),
        state_revision=revision,
        settled_revision=None if owner_waits_for_join else revision,
    )
    frame = IterationFrame(
        iteration_frame_id=current_frame_id,
        loop_instance_id=instance_id,
        iteration_index=0,
        members=tuple(
            IterationMember(token_id=child.token_id, state=IterationMemberState.ACTIVE)
            for child in children
        ),
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
        live_child_token_ids=tuple(sorted(child.token_id for child in children)),
        next_token_ordinal=len(children),
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
        _replace_loop(
            loops,
            _transfer_member_to_children(
                outer,
                owner.token_id,
                tuple(child.token_id for child in children),
                revision,
            ),
        )
    loops.append(loop)
    forks = _reserve_pending_join_owner(snapshot, owner.token_id, revision)
    if fork_id is not None:
        fork = ForkInstance(
            fork_id=fork_id,
            parent_token_id=owner.token_id,
            parent_fork_id=(owner.fork_lineage[-1].fork_id if owner.fork_lineage else None),
            children=tuple(
                ForkChild(token_id=child.token_id, creation_ordinal=ordinal)
                for ordinal, child in enumerate(children)
            ),
            obligations=tuple(
                ForkObligation(
                    obligation_id=_stable_id("obl", snapshot.run_id, fork_id, ordinal),
                    fork_id=fork_id,
                    child_token_id=child.token_id,
                    child_ordinal=ordinal,
                )
                for ordinal, child in enumerate(children)
            ),
            outstanding_child_count=len(children),
            lifecycle_state=ForkLifecycleState.OPEN,
            created_revision=revision,
            updated_revision=revision,
        )
        forks = (*forks, fork)
    return next_snapshot(
        snapshot,
        next_token_ordinal=snapshot.next_token_ordinal + len(children),
        queue=tuple(item for item in snapshot.queue if item.token_id != token_id) + children,
        tokens=(*replace_token(snapshot.tokens, settled_owner), *children),
        forks=forks,
        loops=tuple(loops),
        in_flight_dispatches=tuple(
            item for item in snapshot.in_flight_dispatches if item.dispatch_id != dispatch_id
        ),
    )


__all__ = ["enter_loop"]
