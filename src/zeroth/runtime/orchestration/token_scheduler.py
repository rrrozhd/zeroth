"""Pure state transitions for the structured multi-token scheduler.

This module owns no I/O.  Every transition receives one validated durable
snapshot and returns either its exact replayed value or one complete next
revision.  The small CAS coordinator at the bottom is the only persistence
boundary and depends solely on the runtime-owned store protocol.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, JsonValue

from zeroth.contracts.graph.token_snapshot import (
    TokenEngineSnapshot,
    TokenEngineSnapshotState,
)
from zeroth.contracts.graph.tokens import (
    DispatchLifecycleState,
    ForkChild,
    ForkInstance,
    ForkLifecycleState,
    ForkLineageFrame,
    ForkObligation,
    ForkObligationOutcome,
    InFlightDispatch,
    IterationFrame,
    IterationFrameState,
    IterationMember,
    IterationMemberState,
    LoopInstance,
    SchedulingState,
    TokenEnvelope,
    TokenLifecycleState,
)
from zeroth.runtime.orchestration.token_snapshot_store import (
    TokenSnapshotConcurrencyError,
    TokenSnapshotStore,
)


class TokenSchedulerTransitionError(ValueError):
    """A command cannot be applied to the supplied durable scheduler state."""


class TokenPostCommitError(RuntimeError):
    """A snapshot committed successfully but its post-commit handoff failed."""

    def __init__(self, committed_snapshot: TokenEngineSnapshot, cause: Exception) -> None:
        self.committed_snapshot = committed_snapshot
        self.cause = cause
        super().__init__(f"post-commit effect failed: {cause}")


class FanOutBranch(BaseModel):
    """One ordered child requested by an atomic fan-out transition."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        allow_inf_nan=False,
    )

    node_id: str
    inbound_edge_id: str
    payload: JsonValue


@dataclass(frozen=True, slots=True)
class DispatchClaim:
    """The durable claim snapshot and its exact dispatch record."""

    snapshot: TokenEngineSnapshot
    dispatch: InFlightDispatch


TokenTransition = Callable[[TokenEngineSnapshot | None], TokenEngineSnapshot]
PostCommitEffect = Callable[[TokenEngineSnapshot], Awaitable[None]]


def _stable_id(kind: str, run_id: str, scope_id: str, ordinal: int) -> str:
    material = f"zeroth-token-v1\0{run_id}\0{scope_id}\0{ordinal}".encode()
    digest = hashlib.sha256(material).hexdigest()[:24]
    return f"{kind}_{digest}"


def _model_data(model: BaseModel) -> dict[str, object]:
    data = {name: getattr(model, name) for name in type(model).model_fields}
    if "payload" in data:
        data["payload"] = model.model_dump(mode="json")["payload"]
    return data


def _replace_token(
    tokens: tuple[TokenEnvelope, ...], replacement: TokenEnvelope
) -> tuple[TokenEnvelope, ...]:
    return tuple(
        replacement if token.token_id == replacement.token_id else token for token in tokens
    )


def _next_snapshot(snapshot: TokenEngineSnapshot, **updates: object) -> TokenEngineSnapshot:
    data = _model_data(snapshot)
    data.update(updates)
    data["revision"] = snapshot.revision + 1
    return TokenEngineSnapshot.model_validate(data)


def _updated_token(token: TokenEnvelope, **updates: object) -> TokenEnvelope:
    data = _model_data(token)
    data.update(updates)
    return TokenEnvelope.model_validate(data)


def initialize_token_snapshot(
    *,
    run_id: str,
    root_node_id: str,
    payload: JsonValue,
    causal_inbound_edge_id: str | None = None,
) -> TokenEngineSnapshot:
    """Create the exact revision-zero snapshot for a run's root token."""
    root_scope = f"run:{run_id}"
    root = TokenEnvelope(
        token_id=_stable_id("tok", run_id, root_scope, 0),
        current_node_id=root_node_id,
        causal_inbound_edge_id=causal_inbound_edge_id,
        payload=payload,
        lifecycle_state=TokenLifecycleState.ACTIVE,
        scheduling_state=SchedulingState.QUEUED,
        state_revision=0,
    )
    return TokenEngineSnapshot(
        run_id=run_id,
        revision=0,
        state=TokenEngineSnapshotState.RUNNING,
        next_token_ordinal=1,
        queue=(root,),
        tokens=(root,),
    )


def claim_next_token(snapshot: TokenEngineSnapshot) -> DispatchClaim:
    """Atomically move the canonical queue head into an in-flight dispatch."""
    if snapshot.cancellation_fence is not None and snapshot.cancellation_fence.generation > 0:
        raise TokenSchedulerTransitionError("active cancellation prevents queue claims")
    if snapshot.state is not TokenEngineSnapshotState.RUNNING:
        raise TokenSchedulerTransitionError("tokens may be claimed only from a RUNNING snapshot")
    if not snapshot.queue:
        raise TokenSchedulerTransitionError("no queued token is available to claim")

    queued = snapshot.queue[0]
    revision = snapshot.revision + 1
    executing = _updated_token(
        queued,
        scheduling_state=SchedulingState.EXECUTING,
        state_revision=revision,
    )
    dispatch = InFlightDispatch(
        dispatch_id=_stable_id("dsp", snapshot.run_id, queued.token_id, queued.state_revision),
        idempotency_key=_stable_id("idem", snapshot.run_id, queued.token_id, queued.state_revision),
        token=executing,
        attempt=executing.retry_attempt,
        cancellation_generation=executing.cancellation_generation,
        lifecycle_state=DispatchLifecycleState.EXECUTING,
        started_revision=revision,
        updated_revision=revision,
    )
    next_snapshot = _next_snapshot(
        snapshot,
        queue=snapshot.queue[1:],
        tokens=_replace_token(snapshot.tokens, executing),
        in_flight_dispatches=(*snapshot.in_flight_dispatches, dispatch),
    )
    return DispatchClaim(snapshot=next_snapshot, dispatch=dispatch)


def _matching_dispatch(
    snapshot: TokenEngineSnapshot,
    *,
    dispatch_id: str,
    attempt: int,
    cancellation_generation: int,
) -> InFlightDispatch:
    dispatch = next(
        (item for item in snapshot.in_flight_dispatches if item.dispatch_id == dispatch_id),
        None,
    )
    if dispatch is None:
        raise TokenSchedulerTransitionError(f"dispatch {dispatch_id!r} is not in flight")
    if dispatch.attempt != attempt:
        raise TokenSchedulerTransitionError(
            f"dispatch attempt {attempt} is stale; current attempt is {dispatch.attempt}"
        )
    if dispatch.cancellation_generation != cancellation_generation:
        raise TokenSchedulerTransitionError("dispatch cancellation generation is stale")
    fence_generation = (
        0 if snapshot.cancellation_fence is None else snapshot.cancellation_fence.generation
    )
    if cancellation_generation != fence_generation:
        raise TokenSchedulerTransitionError("dispatch completion generation is stale")
    if dispatch.lifecycle_state is not DispatchLifecycleState.EXECUTING:
        raise TokenSchedulerTransitionError("dispatch is no longer accepting ordinary completion")
    return dispatch


def enqueue_dispatch(
    snapshot: TokenEngineSnapshot,
    *,
    dispatch_id: str,
    attempt: int,
    cancellation_generation: int,
    next_node_id: str,
    inbound_edge_id: str,
    payload: JsonValue,
) -> TokenEngineSnapshot:
    """Complete an ordinary node by moving its token to one successor queue."""
    dispatch = _matching_dispatch(
        snapshot,
        dispatch_id=dispatch_id,
        attempt=attempt,
        cancellation_generation=cancellation_generation,
    )
    revision = snapshot.revision + 1
    queued = _updated_token(
        dispatch.token,
        current_node_id=next_node_id,
        causal_inbound_edge_id=inbound_edge_id,
        payload=payload,
        retry_attempt=0,
        scheduling_state=SchedulingState.QUEUED,
        state_revision=revision,
    )
    return _next_snapshot(
        snapshot,
        queue=(*snapshot.queue, queued),
        tokens=_replace_token(snapshot.tokens, queued),
        in_flight_dispatches=tuple(
            item for item in snapshot.in_flight_dispatches if item.dispatch_id != dispatch_id
        ),
    )


def _settle_innermost_fork(
    snapshot: TokenEngineSnapshot,
    token: TokenEnvelope,
    *,
    outcome: ForkObligationOutcome,
    revision: int,
) -> tuple[ForkInstance, ...]:
    if not token.fork_lineage:
        return snapshot.forks
    owner_id = token.fork_lineage[-1].fork_id
    forks_by_id = {fork.fork_id: fork for fork in snapshot.forks}
    owner = forks_by_id.get(owner_id)
    if owner is None:
        raise TokenSchedulerTransitionError("dispatch token has a missing fork owner")
    obligations: list[ForkObligation] = []
    matched = False
    for obligation in owner.obligations:
        if obligation.child_token_id != token.token_id:
            obligations.append(obligation)
            continue
        if obligation.outcome is not None:
            raise TokenSchedulerTransitionError("fork child obligation is already settled")
        matched = True
        obligation_data = _model_data(obligation)
        obligation_data.update(outcome=outcome, settled_revision=revision)
        obligations.append(ForkObligation.model_validate(obligation_data))
    if not matched:
        raise TokenSchedulerTransitionError("dispatch token has no matching fork obligation")
    outstanding = sum(item.outcome is None for item in obligations)
    owner_data = _model_data(owner)
    owner_data.update(
        obligations=tuple(obligations),
        outstanding_child_count=outstanding,
        lifecycle_state=(ForkLifecycleState.OPEN if outstanding else ForkLifecycleState.CLOSED),
        updated_revision=revision,
        closed_revision=None if outstanding else revision,
    )
    updated_owner = ForkInstance.model_validate(owner_data)
    forks_by_id[owner_id] = updated_owner

    closed = updated_owner
    while closed.lifecycle_state is ForkLifecycleState.CLOSED and closed.parent_fork_id is not None:
        outcomes = {item.outcome for item in closed.obligations}
        if ForkObligationOutcome.JOINED in outcomes:
            # The join transition owns the continuation that will eventually
            # settle the ancestor child. Collapsing it here would consume that
            # lineage before the reducer continuation runs.
            break
        if ForkObligationOutcome.EXITED in outcomes:
            # Exit deliveries are transferred by the owning loop/scope, where
            # their edge-labelled payload collections remain durable.
            break
        if ForkObligationOutcome.FAILED in outcomes:
            aggregate = ForkObligationOutcome.FAILED
        elif ForkObligationOutcome.CANCELLED in outcomes:
            aggregate = ForkObligationOutcome.CANCELLED
        else:
            aggregate = ForkObligationOutcome.SUPPRESSED

        ancestor = forks_by_id.get(closed.parent_fork_id)
        if ancestor is None:
            raise TokenSchedulerTransitionError("closed fork has a missing ancestor owner")
        ancestor_obligations: list[ForkObligation] = []
        matched_ancestor_child = False
        for obligation in ancestor.obligations:
            if obligation.child_token_id != closed.parent_token_id:
                ancestor_obligations.append(obligation)
                continue
            if obligation.outcome is not None:
                raise TokenSchedulerTransitionError(
                    "closed nested fork has an already-settled ancestor obligation"
                )
            matched_ancestor_child = True
            obligation_data = _model_data(obligation)
            obligation_data.update(outcome=aggregate, settled_revision=revision)
            ancestor_obligations.append(ForkObligation.model_validate(obligation_data))
        if not matched_ancestor_child:
            raise TokenSchedulerTransitionError(
                "closed nested fork has no matching ancestor obligation"
            )
        ancestor_outstanding = sum(item.outcome is None for item in ancestor_obligations)
        ancestor_data = _model_data(ancestor)
        ancestor_data.update(
            obligations=tuple(ancestor_obligations),
            outstanding_child_count=ancestor_outstanding,
            lifecycle_state=(
                ForkLifecycleState.OPEN if ancestor_outstanding else ForkLifecycleState.CLOSED
            ),
            updated_revision=revision,
            closed_revision=None if ancestor_outstanding else revision,
        )
        closed = ForkInstance.model_validate(ancestor_data)
        forks_by_id[closed.fork_id] = closed

    return tuple(forks_by_id[fork.fork_id] for fork in snapshot.forks)


def _update_iteration_ownership(
    snapshot: TokenEngineSnapshot,
    token: TokenEnvelope,
    *,
    revision: int,
    parent_state: IterationMemberState,
    child_token_ids: tuple[str, ...] = (),
) -> tuple[LoopInstance, ...]:
    """Settle one member and optionally transfer ownership to fan-out children."""
    if not token.iteration_memberships:
        return snapshot.loops
    memberships = {
        item.loop_instance_id: item.iteration_frame_id for item in token.iteration_memberships
    }
    updated_loops: list[LoopInstance] = []
    for loop in snapshot.loops:
        frame_id = memberships.get(loop.loop_instance_id)
        if frame_id is None:
            updated_loops.append(loop)
            continue
        frames: list[IterationFrame] = []
        matched_frame = False
        for frame in loop.frames:
            if frame.iteration_frame_id != frame_id:
                frames.append(frame)
                continue
            matched_frame = True
            members: list[IterationMember] = []
            matched_parent = False
            for member in frame.members:
                if member.token_id != token.token_id:
                    members.append(member)
                    continue
                if member.state is not IterationMemberState.ACTIVE:
                    raise TokenSchedulerTransitionError("iteration member is already settled")
                matched_parent = True
                members.append(
                    IterationMember(
                        token_id=member.token_id,
                        state=parent_state,
                        settled_revision=revision,
                    )
                )
            if not matched_parent:
                raise TokenSchedulerTransitionError(
                    "dispatch token has no matching active iteration member"
                )
            members.extend(
                IterationMember(token_id=token_id, state=IterationMemberState.ACTIVE)
                for token_id in child_token_ids
            )
            has_active = any(member.state is IterationMemberState.ACTIVE for member in members)
            frame_data = _model_data(frame)
            frame_data.update(
                members=tuple(members),
                state=(
                    IterationFrameState.ACTIVE if has_active else IterationFrameState.BARRIER_READY
                ),
                updated_revision=revision,
            )
            frames.append(IterationFrame.model_validate(frame_data))
        if not matched_frame:
            raise TokenSchedulerTransitionError(
                "dispatch token references a missing iteration frame"
            )
        live_ids = tuple(
            sorted(
                member.token_id
                for frame in frames
                for member in frame.members
                if member.state is IterationMemberState.ACTIVE
            )
        )
        loop_data = _model_data(loop)
        loop_data.update(
            frames=tuple(frames),
            live_child_token_ids=live_ids,
            next_token_ordinal=loop.next_token_ordinal + len(child_token_ids),
            updated_revision=revision,
        )
        updated_loops.append(LoopInstance.model_validate(loop_data))
    return tuple(updated_loops)


def _settle_dispatch(
    snapshot: TokenEngineSnapshot,
    *,
    dispatch_id: str,
    attempt: int,
    cancellation_generation: int,
    fork_outcome: ForkObligationOutcome,
) -> TokenEngineSnapshot:
    dispatch = _matching_dispatch(
        snapshot,
        dispatch_id=dispatch_id,
        attempt=attempt,
        cancellation_generation=cancellation_generation,
    )
    revision = snapshot.revision + 1
    settled = _updated_token(
        dispatch.token,
        lifecycle_state=TokenLifecycleState.SETTLED,
        scheduling_state=SchedulingState.SETTLED,
        state_revision=revision,
        settled_revision=revision,
    )
    forks = _settle_innermost_fork(
        snapshot,
        dispatch.token,
        outcome=fork_outcome,
        revision=revision,
    )
    loops = _update_iteration_ownership(
        snapshot,
        dispatch.token,
        revision=revision,
        parent_state=(
            IterationMemberState.FAILED
            if fork_outcome is ForkObligationOutcome.FAILED
            else IterationMemberState.INTERNAL_COMPLETION
        ),
    )
    return _next_snapshot(
        snapshot,
        tokens=_replace_token(snapshot.tokens, settled),
        forks=forks,
        loops=loops,
        in_flight_dispatches=tuple(
            item for item in snapshot.in_flight_dispatches if item.dispatch_id != dispatch_id
        ),
    )


def complete_dispatch(
    snapshot: TokenEngineSnapshot,
    *,
    dispatch_id: str,
    attempt: int,
    cancellation_generation: int,
) -> TokenEngineSnapshot:
    """Retire an ordinary dispatch after successful terminal completion."""
    return _settle_dispatch(
        snapshot,
        dispatch_id=dispatch_id,
        attempt=attempt,
        cancellation_generation=cancellation_generation,
        fork_outcome=ForkObligationOutcome.SUPPRESSED,
    )


def fail_dispatch(
    snapshot: TokenEngineSnapshot,
    *,
    dispatch_id: str,
    attempt: int,
    cancellation_generation: int,
) -> TokenEngineSnapshot:
    """Retire a dispatch and its innermost fork obligation as failed."""
    return _settle_dispatch(
        snapshot,
        dispatch_id=dispatch_id,
        attempt=attempt,
        cancellation_generation=cancellation_generation,
        fork_outcome=ForkObligationOutcome.FAILED,
    )


def retry_dispatch(
    snapshot: TokenEngineSnapshot,
    *,
    dispatch_id: str,
    attempt: int,
    cancellation_generation: int,
) -> DispatchClaim:
    """Persist a new attempt while retaining stable dispatch/idempotency identity."""
    dispatch = _matching_dispatch(
        snapshot,
        dispatch_id=dispatch_id,
        attempt=attempt,
        cancellation_generation=cancellation_generation,
    )
    revision = snapshot.revision + 1
    executing = _updated_token(
        dispatch.token,
        retry_attempt=dispatch.attempt + 1,
        state_revision=revision,
    )
    retried = InFlightDispatch(
        dispatch_id=dispatch.dispatch_id,
        idempotency_key=dispatch.idempotency_key,
        token=executing,
        attempt=executing.retry_attempt,
        cancellation_generation=dispatch.cancellation_generation,
        lifecycle_state=DispatchLifecycleState.EXECUTING,
        started_revision=revision,
        updated_revision=revision,
    )
    next_snapshot = _next_snapshot(
        snapshot,
        tokens=_replace_token(snapshot.tokens, executing),
        in_flight_dispatches=tuple(
            retried if item.dispatch_id == dispatch_id else item
            for item in snapshot.in_flight_dispatches
        ),
    )
    return DispatchClaim(snapshot=next_snapshot, dispatch=retried)


def recover_dispatch(snapshot: TokenEngineSnapshot, *, dispatch_id: str) -> DispatchClaim:
    """Fence a crash-ambiguous dispatch with a new durable attempt."""
    dispatch = next(
        (item for item in snapshot.in_flight_dispatches if item.dispatch_id == dispatch_id),
        None,
    )
    if dispatch is None:
        raise TokenSchedulerTransitionError(f"dispatch {dispatch_id!r} is not in flight")
    return retry_dispatch(
        snapshot,
        dispatch_id=dispatch_id,
        attempt=dispatch.attempt,
        cancellation_generation=dispatch.cancellation_generation,
    )


def _fanout_fork_id(
    snapshot: TokenEngineSnapshot,
    dispatch_id: str,
    branches: Sequence[FanOutBranch],
) -> str:
    branch_state = json.dumps(
        [branch.model_dump(mode="json") for branch in branches],
        sort_keys=True,
        separators=(",", ":"),
    )
    material = f"zeroth-fanout-v1\0{snapshot.run_id}\0{dispatch_id}\0{branch_state}".encode()
    return f"fork_{hashlib.sha256(material).hexdigest()[:24]}"


def _validate_fanout_replay(
    fork: ForkInstance,
    branches: Sequence[FanOutBranch],
) -> None:
    if len(fork.children) != len(branches):
        raise TokenSchedulerTransitionError("fan-out replay contradicts persisted child count")


def fan_out_dispatch(
    snapshot: TokenEngineSnapshot,
    *,
    dispatch_id: str,
    attempt: int,
    cancellation_generation: int,
    branches: Sequence[FanOutBranch],
) -> TokenEngineSnapshot:
    """Atomically retire a parent and publish its exact ordered child cohort."""
    if not branches:
        raise TokenSchedulerTransitionError("fan-out requires at least one child branch")
    fork_id = _fanout_fork_id(snapshot, dispatch_id, branches)
    persisted = next((fork for fork in snapshot.forks if fork.fork_id == fork_id), None)
    if persisted is not None:
        _validate_fanout_replay(persisted, branches)
        return snapshot
    if snapshot.cancellation_fence is not None and snapshot.cancellation_fence.generation > 0:
        raise TokenSchedulerTransitionError("active cancellation prevents new child creation")
    dispatch = _matching_dispatch(
        snapshot,
        dispatch_id=dispatch_id,
        attempt=attempt,
        cancellation_generation=cancellation_generation,
    )
    revision = snapshot.revision + 1
    parent = _updated_token(
        dispatch.token,
        lifecycle_state=TokenLifecycleState.SETTLED,
        scheduling_state=SchedulingState.SETTLED,
        state_revision=revision,
        settled_revision=revision,
    )
    parent_fork_id = None if not parent.fork_lineage else parent.fork_lineage[-1].fork_id
    children: list[TokenEnvelope] = []
    fork_children: list[ForkChild] = []
    obligations: list[ForkObligation] = []
    allocation_scope = f"run:{snapshot.run_id}"
    allocation_cursor = snapshot.next_token_ordinal
    if parent.iteration_memberships:
        loop_id = parent.iteration_memberships[-1].loop_instance_id
        loop_owner = next(
            (loop for loop in snapshot.loops if loop.loop_instance_id == loop_id),
            None,
        )
        if loop_owner is None:
            raise TokenSchedulerTransitionError("fan-out token has a missing loop owner")
        allocation_scope = f"loop:{loop_id}"
        allocation_cursor = loop_owner.next_token_ordinal
    for child_ordinal, branch in enumerate(branches):
        allocation_ordinal = allocation_cursor + child_ordinal
        token_id = _stable_id("tok", snapshot.run_id, allocation_scope, allocation_ordinal)
        child = TokenEnvelope(
            token_id=token_id,
            parent_token_id=parent.token_id,
            provenance_tag=parent.provenance_tag,
            current_node_id=branch.node_id,
            causal_inbound_edge_id=branch.inbound_edge_id,
            payload=branch.payload,
            lifecycle_state=TokenLifecycleState.ACTIVE,
            scheduling_state=SchedulingState.QUEUED,
            fork_lineage=(
                *parent.fork_lineage,
                ForkLineageFrame(
                    fork_id=fork_id,
                    parent_fork_id=parent_fork_id,
                    child_ordinal=child_ordinal,
                ),
            ),
            iteration_memberships=parent.iteration_memberships,
            cancellation_generation=parent.cancellation_generation,
            state_revision=revision,
        )
        children.append(child)
        fork_children.append(ForkChild(token_id=token_id, creation_ordinal=child_ordinal))
        obligations.append(
            ForkObligation(
                obligation_id=_stable_id("obl", snapshot.run_id, fork_id, child_ordinal),
                fork_id=fork_id,
                child_token_id=token_id,
                child_ordinal=child_ordinal,
            )
        )
    fork = ForkInstance(
        fork_id=fork_id,
        parent_token_id=parent.token_id,
        parent_fork_id=parent_fork_id,
        children=tuple(fork_children),
        obligations=tuple(obligations),
        outstanding_child_count=len(children),
        lifecycle_state=ForkLifecycleState.OPEN,
        created_revision=revision,
        updated_revision=revision,
    )
    loops = _update_iteration_ownership(
        snapshot,
        dispatch.token,
        revision=revision,
        parent_state=IterationMemberState.INTERNAL_COMPLETION,
        child_token_ids=tuple(child.token_id for child in children),
    )
    return _next_snapshot(
        snapshot,
        next_token_ordinal=snapshot.next_token_ordinal + len(children),
        queue=(*snapshot.queue, *children),
        tokens=(*_replace_token(snapshot.tokens, parent), *children),
        forks=(*snapshot.forks, fork),
        loops=loops,
        in_flight_dispatches=tuple(
            item for item in snapshot.in_flight_dispatches if item.dispatch_id != dispatch_id
        ),
    )


async def apply_token_transition(
    store: TokenSnapshotStore,
    run_id: str,
    transition: TokenTransition,
    *,
    after_commit: PostCommitEffect | None = None,
    max_attempts: int = 8,
) -> TokenEngineSnapshot:
    """Reload and reapply a pure transition until its snapshot CAS succeeds.

    ``after_commit`` is deliberately outside the retry loop's mutation step and
    runs only once, after this coordinator wins CAS.  Callers can therefore use
    it to start external dispatch work without replaying that work on contention.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    last_error: TokenSnapshotConcurrencyError | None = None
    for _ in range(max_attempts):
        current = await store.get_token_snapshot(run_id)
        proposed = transition(current)
        if current is not None and proposed == current:
            return current
        expected_revision = None if current is None else current.revision
        try:
            committed = await store.compare_and_swap_token_snapshot(
                run_id,
                expected_revision=expected_revision,
                snapshot=proposed,
            )
        except TokenSnapshotConcurrencyError as exc:
            last_error = exc
            continue
        if after_commit is not None:
            try:
                await after_commit(committed)
            except Exception as exc:
                raise TokenPostCommitError(committed, exc) from exc
        return committed
    assert last_error is not None
    raise last_error


__all__ = [
    "DispatchClaim",
    "FanOutBranch",
    "PostCommitEffect",
    "TokenPostCommitError",
    "TokenSchedulerTransitionError",
    "TokenTransition",
    "apply_token_transition",
    "claim_next_token",
    "complete_dispatch",
    "enqueue_dispatch",
    "fail_dispatch",
    "fan_out_dispatch",
    "initialize_token_snapshot",
    "recover_dispatch",
    "retry_dispatch",
]
