"""Durable lifecycle transitions for structured-token snapshots.

The scheduler owns node-to-node execution transitions.  This module owns the
orthogonal operator lifecycle: pausing dispatch, recording a replayable stop,
and fencing cancellation across queued, waiting, and executing tokens.
"""

from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel

from zeroth.contracts.graph.token_snapshot import TokenEngineSnapshot, TokenEngineSnapshotState
from zeroth.contracts.graph.tokens import (
    CancellationFence,
    DispatchLifecycleState,
    ForkInstance,
    ForkLifecycleState,
    ForkObligation,
    ForkObligationOutcome,
    InFlightDispatch,
    IterationFrame,
    IterationFrameState,
    IterationMember,
    IterationMemberState,
    JoinInstance,
    JoinLifecycleState,
    JoinObligation,
    JoinObligationOutcome,
    LoopInstance,
    SchedulingState,
    TokenEnvelope,
    TokenLifecycleState,
)
from zeroth.runtime.orchestration.token_scheduler import TokenSchedulerTransitionError
from zeroth.runtime.orchestration.token_snapshot_store import (
    TokenSnapshotConcurrencyError,
    TokenSnapshotStore,
)

TokenLifecycleTransition = Callable[[TokenEngineSnapshot], TokenEngineSnapshot]


def _data(model: BaseModel) -> dict[str, object]:
    data = {name: getattr(model, name) for name in type(model).model_fields}
    if "payload" in data:
        data["payload"] = model.model_dump(mode="json")["payload"]
    return data


def _next(snapshot: TokenEngineSnapshot, **updates: object) -> TokenEngineSnapshot:
    data = _data(snapshot)
    data.update(updates, revision=snapshot.revision + 1)
    return TokenEngineSnapshot.model_validate(data)


def pause_snapshot(snapshot: TokenEngineSnapshot) -> TokenEngineSnapshot:
    """Freeze new dispatch while preserving every token and owner verbatim."""
    if snapshot.state is TokenEngineSnapshotState.PAUSED:
        return snapshot
    if snapshot.state is not TokenEngineSnapshotState.RUNNING:
        raise TokenSchedulerTransitionError(f"cannot pause a {snapshot.state.value} token snapshot")
    return _next(snapshot, state=TokenEngineSnapshotState.PAUSED)


def resume_snapshot(snapshot: TokenEngineSnapshot) -> TokenEngineSnapshot:
    """Resume a paused or replayable stopped snapshot without rebuilding work."""
    if snapshot.state is TokenEngineSnapshotState.RUNNING:
        return snapshot
    if snapshot.state not in {
        TokenEngineSnapshotState.PAUSED,
        TokenEngineSnapshotState.STOPPED,
    }:
        raise TokenSchedulerTransitionError(
            f"cannot resume a {snapshot.state.value} token snapshot"
        )
    return _next(snapshot, state=TokenEngineSnapshotState.RUNNING)


def stop_snapshot(snapshot: TokenEngineSnapshot) -> TokenEngineSnapshot:
    """Prevent new top-level work and drain already-owned structured scopes."""
    if snapshot.state is TokenEngineSnapshotState.STOPPED:
        return snapshot
    if snapshot.state not in {
        TokenEngineSnapshotState.RUNNING,
        TokenEngineSnapshotState.PAUSED,
        TokenEngineSnapshotState.STOPPING,
    }:
        raise TokenSchedulerTransitionError(f"cannot stop a {snapshot.state.value} token snapshot")
    needs_drain = bool(snapshot.in_flight_dispatches) or any(
        token.fork_lineage or token.iteration_memberships for token in snapshot.queue
    )
    if snapshot.state is not TokenEngineSnapshotState.PAUSED and needs_drain:
        if snapshot.state is TokenEngineSnapshotState.STOPPING:
            return snapshot
        return _next(snapshot, state=TokenEngineSnapshotState.STOPPING)
    return _next(snapshot, state=TokenEngineSnapshotState.STOPPED)


def _cancelled_token(
    token: TokenEnvelope,
    *,
    generation: int,
    revision: int,
    acknowledged: bool,
) -> TokenEnvelope:
    data = _data(token)
    data.update(
        lifecycle_state=TokenLifecycleState.SETTLED,
        scheduling_state=SchedulingState.SETTLED,
        cancellation_generation=generation,
        cancellation_acknowledged_generation=(generation if acknowledged else None),
        state_revision=revision,
        settled_revision=revision,
    )
    return TokenEnvelope.model_validate(data)


def _cancel_forks(
    forks: tuple[ForkInstance, ...], token_ids: set[str], *, revision: int
) -> tuple[ForkInstance, ...]:
    by_id = {fork.fork_id: fork for fork in forks}
    changed = True
    while changed:
        changed = False
        for fork_id, fork in tuple(by_id.items()):
            obligations: list[ForkObligation] = []
            fork_changed = False
            for obligation in fork.obligations:
                if obligation.child_token_id not in token_ids:
                    obligations.append(obligation)
                    continue
                if obligation.outcome is not ForkObligationOutcome.CANCELLED:
                    obligation = ForkObligation.model_validate(
                        {
                            **_data(obligation),
                            "outcome": ForkObligationOutcome.CANCELLED,
                            "join_instance_id": None,
                            "settled_revision": revision,
                        }
                    )
                    fork_changed = True
                obligations.append(obligation)
            if not fork_changed:
                continue
            outstanding = sum(item.outcome is None for item in obligations)
            closed = outstanding == 0
            updated = ForkInstance.model_validate(
                {
                    **_data(fork),
                    "obligations": tuple(obligations),
                    "outstanding_child_count": outstanding,
                    "lifecycle_state": (
                        ForkLifecycleState.CLOSED if closed else ForkLifecycleState.OPEN
                    ),
                    "updated_revision": revision,
                    "closed_revision": revision if closed else None,
                }
            )
            by_id[fork_id] = updated
            if closed and updated.parent_fork_id is not None:
                token_ids.add(updated.parent_token_id)
            changed = True
    return tuple(by_id[fork.fork_id] for fork in forks)


def _cancel_joins(
    joins: tuple[JoinInstance, ...], token_ids: set[str], *, revision: int
) -> tuple[JoinInstance, ...]:
    updated_joins: list[JoinInstance] = []
    for join in joins:
        obligations: list[JoinObligation] = []
        changed = False
        for obligation in join.obligations:
            if obligation.source_token_id not in token_ids:
                obligations.append(obligation)
                continue
            changed = True
            obligations.append(
                JoinObligation.model_validate(
                    {
                        **_data(obligation),
                        "outcome": JoinObligationOutcome.CANCELLED,
                        "delivery": None,
                        "settled_revision": revision,
                    }
                )
            )
        if not changed:
            updated_joins.append(join)
            continue
        all_settled = all(item.outcome is not None for item in obligations)
        updated_joins.append(
            JoinInstance.model_validate(
                {
                    **_data(join),
                    "obligations": tuple(obligations),
                    "lifecycle_state": (
                        JoinLifecycleState.CLOSED if all_settled else JoinLifecycleState.OPEN
                    ),
                    "consumed_parent_token_ids": (
                        tuple(item.source_token_id for item in obligations) if all_settled else ()
                    ),
                    "continuation_token_id": None,
                    "updated_revision": revision,
                    "closed_revision": revision if all_settled else None,
                }
            )
        )
    return tuple(updated_joins)


def _cancel_loops(
    loops: tuple[LoopInstance, ...], token_ids: set[str], *, revision: int
) -> tuple[LoopInstance, ...]:
    updated_loops: list[LoopInstance] = []
    for loop in loops:
        frames: list[IterationFrame] = []
        changed = False
        for frame in loop.frames:
            members: list[IterationMember] = []
            for member in frame.members:
                if (
                    member.token_id not in token_ids
                    or member.state is not IterationMemberState.ACTIVE
                ):
                    members.append(member)
                    continue
                changed = True
                members.append(
                    IterationMember(
                        token_id=member.token_id,
                        state=IterationMemberState.CANCELLED,
                        settled_revision=revision,
                    )
                )
            active = any(item.state is IterationMemberState.ACTIVE for item in members)
            frames.append(
                IterationFrame.model_validate(
                    {
                        **_data(frame),
                        "members": tuple(members),
                        "state": (
                            IterationFrameState.ACTIVE
                            if active
                            else IterationFrameState.BARRIER_READY
                        ),
                        "updated_revision": revision if changed else frame.updated_revision,
                    }
                )
            )
        if not changed:
            updated_loops.append(loop)
            continue
        live_ids = tuple(
            sorted(
                member.token_id
                for frame in frames
                for member in frame.members
                if member.state is IterationMemberState.ACTIVE
            )
        )
        updated_loops.append(
            LoopInstance.model_validate(
                {
                    **_data(loop),
                    "frames": tuple(frames),
                    "live_child_token_ids": live_ids,
                    "updated_revision": revision,
                }
            )
        )
    return tuple(updated_loops)


def _terminal_cancelled(
    snapshot: TokenEngineSnapshot,
    *,
    generation: int,
    requested_revision: int,
    acknowledged_token_ids: tuple[str, ...],
    acknowledged_dispatch_ids: tuple[str, ...] = (),
) -> TokenEngineSnapshot:
    revision = snapshot.revision + 1
    return _next(
        snapshot,
        state=TokenEngineSnapshotState.CANCELLED,
        queue=(),
        tokens=(),
        forks=(),
        joins=(),
        loops=(),
        in_flight_dispatches=(),
        cancellation_fence=CancellationFence(
            generation=generation,
            requested_revision=requested_revision,
            acknowledged_token_ids=tuple(sorted(acknowledged_token_ids)),
            acknowledged_dispatch_ids=tuple(sorted(acknowledged_dispatch_ids)),
            state_revision=revision,
        ),
    )


def request_cancellation(snapshot: TokenEngineSnapshot) -> TokenEngineSnapshot:
    """Fence old completions and cancel every non-executing child atomically."""
    if snapshot.state is TokenEngineSnapshotState.CANCELLED:
        return snapshot
    if snapshot.state in {
        TokenEngineSnapshotState.COMPLETED,
        TokenEngineSnapshotState.FAILED,
    }:
        raise TokenSchedulerTransitionError(
            f"cannot cancel a {snapshot.state.value} token snapshot"
        )
    previous_generation = (
        0 if snapshot.cancellation_fence is None else snapshot.cancellation_fence.generation
    )
    generation = previous_generation + 1
    requested_revision = snapshot.revision + 1
    if not snapshot.in_flight_dispatches:
        return _terminal_cancelled(
            snapshot,
            generation=generation,
            requested_revision=requested_revision,
            acknowledged_token_ids=(),
        )

    executing_ids = {item.token.token_id for item in snapshot.in_flight_dispatches}
    cancelled_ids = {
        token.token_id
        for token in snapshot.tokens
        if token.scheduling_state is not SchedulingState.SETTLED
        and token.token_id not in executing_ids
    }
    tokens = tuple(
        _cancelled_token(
            token,
            generation=generation,
            revision=requested_revision,
            acknowledged=token.scheduling_state is SchedulingState.JOIN_WAITING,
        )
        if token.token_id in cancelled_ids
        else token
        for token in snapshot.tokens
    )
    token_by_id = {token.token_id: token for token in tokens}
    dispatches = tuple(
        InFlightDispatch.model_validate(
            {
                **_data(dispatch),
                "token": token_by_id[dispatch.token.token_id],
                "lifecycle_state": DispatchLifecycleState.CANCELLATION_REQUESTED,
                "cancellation_requested_generation": generation,
                "cancellation_requested_revision": requested_revision,
                "updated_revision": requested_revision,
            }
        )
        for dispatch in snapshot.in_flight_dispatches
    )
    structured_cancelled_ids = set(cancelled_ids)
    return _next(
        snapshot,
        state=TokenEngineSnapshotState.RUNNING,
        queue=(),
        tokens=tokens,
        forks=_cancel_forks(snapshot.forks, structured_cancelled_ids, revision=requested_revision),
        joins=_cancel_joins(snapshot.joins, structured_cancelled_ids, revision=requested_revision),
        loops=_cancel_loops(snapshot.loops, structured_cancelled_ids, revision=requested_revision),
        in_flight_dispatches=dispatches,
        cancellation_fence=CancellationFence(
            generation=generation,
            requested_revision=requested_revision,
            state_revision=requested_revision,
        ),
    )


def acknowledge_cancellation(
    snapshot: TokenEngineSnapshot,
    *,
    dispatch_id: str,
    cancellation_generation: int,
) -> TokenEngineSnapshot:
    """Settle one executing child and compact when the final child acknowledges."""
    fence = snapshot.cancellation_fence
    if fence is None or fence.generation != cancellation_generation:
        raise TokenSchedulerTransitionError("cancellation acknowledgement generation is stale")
    if snapshot.state is TokenEngineSnapshotState.CANCELLED:
        return snapshot
    dispatch = next(
        (item for item in snapshot.in_flight_dispatches if item.dispatch_id == dispatch_id), None
    )
    if dispatch is None:
        if dispatch_id in fence.acknowledged_dispatch_ids:
            return snapshot
        raise TokenSchedulerTransitionError(
            f"dispatch {dispatch_id!r} is not awaiting cancellation"
        )
    if dispatch.lifecycle_state is not DispatchLifecycleState.CANCELLATION_REQUESTED:
        raise TokenSchedulerTransitionError("dispatch has no durable cancellation request")
    revision = snapshot.revision + 1
    token_id = dispatch.token.token_id
    acknowledged_ids = tuple(sorted((*fence.acknowledged_token_ids, token_id)))
    acknowledged_dispatch_ids = tuple(
        sorted((*fence.acknowledged_dispatch_ids, dispatch_id))
    )
    remaining = tuple(
        item for item in snapshot.in_flight_dispatches if item.dispatch_id != dispatch_id
    )
    if not remaining:
        return _terminal_cancelled(
            snapshot,
            generation=cancellation_generation,
            requested_revision=fence.requested_revision or revision,
            acknowledged_token_ids=acknowledged_ids,
            acknowledged_dispatch_ids=acknowledged_dispatch_ids,
        )

    tokens = tuple(
        _cancelled_token(
            token,
            generation=cancellation_generation,
            revision=revision,
            acknowledged=True,
        )
        if token.token_id == token_id
        else token
        for token in snapshot.tokens
    )
    cancelled_ids = {token_id}
    return _next(
        snapshot,
        tokens=tokens,
        forks=_cancel_forks(snapshot.forks, cancelled_ids, revision=revision),
        joins=_cancel_joins(snapshot.joins, cancelled_ids, revision=revision),
        loops=_cancel_loops(snapshot.loops, cancelled_ids, revision=revision),
        in_flight_dispatches=remaining,
        cancellation_fence=CancellationFence(
            generation=cancellation_generation,
            requested_revision=fence.requested_revision,
            acknowledged_token_ids=acknowledged_ids,
            acknowledged_dispatch_ids=acknowledged_dispatch_ids,
            state_revision=revision,
        ),
    )


class TokenLifecycleAdapter:
    """CAS-retrying persistence boundary for lifecycle transitions."""

    def __init__(self, store: TokenSnapshotStore) -> None:
        self.store = store

    async def _apply(
        self, run_id: str, transition: TokenLifecycleTransition
    ) -> TokenEngineSnapshot:
        while True:
            current = await self.store.get_token_snapshot(run_id)
            if current is None:
                raise KeyError(f"token snapshot for run {run_id!r} does not exist")
            proposed = transition(current)
            if proposed is current:
                return current
            try:
                return await self.store.compare_and_swap_token_snapshot(
                    run_id,
                    expected_revision=current.revision,
                    snapshot=proposed,
                )
            except TokenSnapshotConcurrencyError:
                continue

    async def pause(self, run_id: str) -> TokenEngineSnapshot:
        return await self._apply(run_id, pause_snapshot)

    async def resume(self, run_id: str) -> TokenEngineSnapshot:
        return await self._apply(run_id, resume_snapshot)

    async def stop(self, run_id: str) -> TokenEngineSnapshot:
        return await self._apply(run_id, stop_snapshot)

    async def cancel(self, run_id: str) -> TokenEngineSnapshot:
        return await self._apply(run_id, request_cancellation)

    async def acknowledge(
        self, run_id: str, *, dispatch_id: str, cancellation_generation: int
    ) -> TokenEngineSnapshot:
        return await self._apply(
            run_id,
            lambda snapshot: acknowledge_cancellation(
                snapshot,
                dispatch_id=dispatch_id,
                cancellation_generation=cancellation_generation,
            ),
        )


__all__ = [
    "TokenLifecycleAdapter",
    "acknowledge_cancellation",
    "pause_snapshot",
    "request_cancellation",
    "resume_snapshot",
    "stop_snapshot",
]
