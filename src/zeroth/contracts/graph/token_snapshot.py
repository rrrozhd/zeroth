"""Coherent, replayable persistence contract for the token engine."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from zeroth.contracts.graph.tokens import (
    CancellationFence,
    DispatchLifecycleState,
    ForkInstance,
    ForkObligationOutcome,
    InFlightDispatch,
    IterationMemberState,
    JoinInstance,
    JoinLifecycleState,
    JoinObligationOutcome,
    LoopExitResolutionOutcome,
    LoopInstance,
    LoopLifecycleState,
    SchedulingState,
    StateRevision,
    TokenEnvelope,
    TokenId,
)

SnapshotSchemaVersion = Literal[1]
TokenOrdinal = Annotated[int, Field(ge=0)]


class TokenEngineSnapshotState(StrEnum):
    """Lifecycle state represented by a durable engine snapshot."""

    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


_TERMINAL_STATES = {
    TokenEngineSnapshotState.COMPLETED,
    TokenEngineSnapshotState.CANCELLED,
    TokenEngineSnapshotState.FAILED,
}


def _require_unique(values: tuple[str, ...], description: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{description} must use unique IDs")


def _has_cycle(parents: dict[str, tuple[str, ...]]) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(current: str) -> bool:
        if current in visiting:
            return True
        if current in visited or current not in parents:
            return False
        visiting.add(current)
        if any(visit(parent) for parent in parents[current]):
            return True
        visiting.remove(current)
        visited.add(current)
        return False

    return any(visit(start) for start in parents)


class TokenEngineSnapshot(BaseModel):
    """One atomic token-engine checkpoint, suitable for exact replay.

    Queue and dispatch entries deliberately retain complete envelopes. The
    snapshot validates those copies against the canonical ``tokens`` entry so
    replay never has to reconstruct payload or provenance from node metadata.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        allow_inf_nan=False,
    )

    schema_version: SnapshotSchemaVersion = 1
    run_id: Annotated[str, Field(min_length=1, pattern=r"^\S+$")]
    revision: StateRevision
    state: TokenEngineSnapshotState
    failure_mode: Literal["fail_fast", "best_effort"] = "fail_fast"
    next_token_ordinal: TokenOrdinal
    queue: tuple[TokenEnvelope, ...] = ()
    tokens: tuple[TokenEnvelope, ...] = ()
    forks: tuple[ForkInstance, ...] = ()
    joins: tuple[JoinInstance, ...] = ()
    loops: tuple[LoopInstance, ...] = ()
    cancellation_fence: CancellationFence | None = None
    in_flight_dispatches: tuple[InFlightDispatch, ...] = ()

    @model_validator(mode="after")
    def _validate_snapshot(self) -> TokenEngineSnapshot:
        token_ids = tuple(token.token_id for token in self.tokens)
        queued_ids = tuple(token.token_id for token in self.queue)
        fork_ids = tuple(item.fork_id for item in self.forks)
        join_ids = tuple(item.join_instance_id for item in self.joins)
        loop_ids = tuple(item.loop_instance_id for item in self.loops)
        dispatch_ids = tuple(item.dispatch_id for item in self.in_flight_dispatches)
        frame_ids = tuple(frame.iteration_frame_id for loop in self.loops for frame in loop.frames)
        obligation_ids = tuple(
            obligation.obligation_id for fork in self.forks for obligation in fork.obligations
        ) + tuple(
            obligation.obligation_id for join in self.joins for obligation in join.obligations
        )
        _require_unique(token_ids, "durable token IDs")
        _require_unique(queued_ids, "queued token IDs")
        _require_unique(fork_ids, "fork IDs")
        _require_unique(join_ids, "join IDs")
        _require_unique(loop_ids, "loop IDs")
        _require_unique(dispatch_ids, "dispatch IDs")
        _require_unique(frame_ids, "iteration frame IDs")
        _require_unique(obligation_ids, "structured obligation IDs")

        if self.state in {
            TokenEngineSnapshotState.COMPLETED,
            TokenEngineSnapshotState.FAILED,
        } and any(
            (
                self.queue,
                self.tokens,
                self.forks,
                self.joins,
                self.loops,
                self.in_flight_dispatches,
            )
        ):
            raise ValueError("a terminal snapshot must contain no token-engine work state")
        if self.state is TokenEngineSnapshotState.CANCELLED:
            if self.queue or self.in_flight_dispatches:
                raise ValueError("a CANCELLED snapshot cannot contain schedulable work")
            if any(token.scheduling_state is not SchedulingState.SETTLED for token in self.tokens):
                raise ValueError("a CANCELLED snapshot may retain only settled tokens")
            if any(
                obligation.outcome is None
                for fork in self.forks
                for obligation in fork.obligations
            ):
                raise ValueError("a CANCELLED snapshot cannot retain open fork obligations")
            if any(
                obligation.outcome is None
                for join in self.joins
                for obligation in join.obligations
            ):
                raise ValueError("a CANCELLED snapshot cannot retain open join obligations")
            if any(
                join.lifecycle_state
                not in {JoinLifecycleState.CANCELLED, JoinLifecycleState.CLOSED}
                for join in self.joins
            ):
                raise ValueError("a CANCELLED snapshot cannot retain nonterminal joins")
            if any(
                member.state is IterationMemberState.ACTIVE
                for loop in self.loops
                for frame in loop.frames
                for member in frame.members
            ):
                raise ValueError("a CANCELLED snapshot cannot retain active iteration members")
            if any(
                loop.lifecycle_state
                not in {LoopLifecycleState.CANCELLED, LoopLifecycleState.COMPLETED}
                for loop in self.loops
            ):
                raise ValueError("a CANCELLED snapshot cannot retain nonterminal loops")

        tokens = {token.token_id: token for token in self.tokens}
        forks = {item.fork_id: item for item in self.forks}
        loops = {item.loop_instance_id: item for item in self.loops}
        frames = {
            frame.iteration_frame_id: (loop, frame) for loop in self.loops for frame in loop.frames
        }

        if self.next_token_ordinal < len(tokens):
            raise ValueError("next_token_ordinal cannot precede durable token allocation")

        for queued in self.queue:
            canonical = tokens.get(queued.token_id)
            if canonical is None:
                raise ValueError("queued token is missing from durable tokens")
            if queued != canonical:
                raise ValueError("queued token envelope must exactly match its durable token")

        dispatch_locations: dict[TokenId, int] = {}
        cancellation_requested_token_ids: set[TokenId] = set()
        cancellation_generation = (
            0 if self.cancellation_fence is None else self.cancellation_fence.generation
        )
        for dispatch in self.in_flight_dispatches:
            canonical = tokens.get(dispatch.token.token_id)
            if canonical is None:
                raise ValueError("in-flight dispatch references a missing durable token")
            if dispatch.token != canonical:
                raise ValueError("in-flight dispatch token must exactly match its durable token")
            dispatch_locations[dispatch.token.token_id] = (
                dispatch_locations.get(dispatch.token.token_id, 0) + 1
            )
            if dispatch.lifecycle_state is DispatchLifecycleState.CANCELLATION_REQUESTED:
                fence = self.cancellation_fence
                if (
                    fence is None
                    or dispatch.cancellation_generation >= fence.generation
                    or dispatch.cancellation_requested_generation != fence.generation
                    or dispatch.cancellation_requested_revision != fence.requested_revision
                ):
                    raise ValueError(
                        "cancellation-requested dispatch must match the durable cancellation fence"
                    )
                cancellation_requested_token_ids.add(dispatch.token.token_id)
            elif dispatch.cancellation_generation != cancellation_generation:
                raise ValueError(
                    "ordinary executing dispatch must match the durable cancellation fence"
                )

        if any(
            token.scheduling_state is not SchedulingState.SETTLED
            and token.token_id not in cancellation_requested_token_ids
            and token.cancellation_generation != cancellation_generation
            for token in self.tokens
        ):
            raise ValueError("every live token must match the durable cancellation fence")

        waiting_locations: dict[TokenId, int] = {}
        for join in self.joins:
            fork = forks.get(join.fork_id)
            if fork is None:
                raise ValueError("join instance references a missing fork")
            if join.continuation_token_id is not None and join.continuation_token_id not in tokens:
                raise ValueError("join references a missing continuation token")
            if join.continuation_token_id is not None and (
                tokens[join.continuation_token_id].continuation_parent_token_ids
                != join.consumed_parent_token_ids
            ):
                raise ValueError(
                    "join continuation parentage must exactly match consumed parent tokens"
                )
            if any(token_id not in tokens for token_id in join.consumed_parent_token_ids):
                raise ValueError("join references a missing consumed parent token")
            for obligation in join.obligations:
                source = tokens.get(obligation.source_token_id)
                if source is None:
                    raise ValueError("join obligation references a missing source token")
                if source.provenance_tag != join.provenance_tag:
                    raise ValueError(
                        "join obligation source provenance must exactly match its join"
                    )
                if source.iteration_memberships != join.iteration_memberships:
                    raise ValueError(
                        "join obligation source iteration membership must exactly match its join"
                    )
                if join.lifecycle_state in {
                    JoinLifecycleState.CANCELLED,
                    JoinLifecycleState.CLOSED,
                }:
                    if source.scheduling_state is not SchedulingState.SETTLED:
                        raise ValueError("a settled join obligation source must be durably SETTLED")
                elif (
                    source.scheduling_state is SchedulingState.SETTLED
                    and (
                        (
                            self.state is TokenEngineSnapshotState.CANCELLED
                            and obligation.outcome is not None
                        )
                        or (
                            obligation.outcome is JoinObligationOutcome.CANCELLED
                            and (
                                source.cancellation_acknowledged_generation is not None
                                or join.failure_mode == "fail_fast"
                            )
                        )
                    )
                ):
                    pass
                elif obligation.outcome is not None or (
                    obligation.outcome is None
                    and source.scheduling_state is SchedulingState.JOIN_WAITING
                ):
                    if source.scheduling_state is not SchedulingState.JOIN_WAITING:
                        raise ValueError(
                            "an arrived OPEN/READY join source must remain durably JOIN_WAITING"
                        )
                    waiting_locations[source.token_id] = (
                        waiting_locations.get(source.token_id, 0) + 1
                    )
                elif source.scheduling_state is SchedulingState.SETTLED:
                    raise ValueError(
                        "an unsettled join obligation source cannot already be settled"
                    )
                fork_matches = tuple(
                    item
                    for item in fork.obligations
                    if item.child_token_id == obligation.source_token_id
                    and item.child_ordinal == obligation.child_ordinal
                )
                if len(fork_matches) != 1:
                    raise ValueError(
                        "join obligation must match exactly one fork cohort obligation"
                    )
                fork_outcome = fork_matches[0].outcome
                expected_fork_outcome = {
                    None: None,
                    JoinObligationOutcome.DELIVERED: ForkObligationOutcome.JOINED,
                    JoinObligationOutcome.SUPPRESSED: ForkObligationOutcome.SUPPRESSED,
                    JoinObligationOutcome.FAILED: ForkObligationOutcome.FAILED,
                    JoinObligationOutcome.CANCELLED: ForkObligationOutcome.CANCELLED,
                }[obligation.outcome]
                if (
                    obligation.outcome is None
                    and source.scheduling_state is SchedulingState.JOIN_WAITING
                ):
                    # Schema-v1 snapshots produced by the earlier barrier port
                    # marked fork ownership JOINED before attaching delivery.
                    expected_fork_outcome = ForkObligationOutcome.JOINED
                if fork_outcome is not expected_fork_outcome:
                    raise ValueError("join and fork obligation outcomes contradict")
                expected_join_id = (
                    join.join_instance_id
                    if expected_fork_outcome is ForkObligationOutcome.JOINED
                    else None
                )
                if fork_matches[0].join_instance_id != expected_join_id:
                    raise ValueError("fork join ownership contradicts its delivered resolution")
            joined_fork_keys = {
                (item.child_token_id, item.child_ordinal)
                for item in fork.obligations
                if item.outcome is ForkObligationOutcome.JOINED
                and item.join_instance_id == join.join_instance_id
            }
            join_keys = {
                (item.source_token_id, item.child_ordinal)
                for item in join.obligations
                if item.outcome is JoinObligationOutcome.DELIVERED
                or (
                    item.outcome is None
                    and tokens[item.source_token_id].scheduling_state
                    is SchedulingState.JOIN_WAITING
                )
            }
            if joined_fork_keys != join_keys:
                raise ValueError(
                    "JOINED fork obligations and join obligations must form a bijection"
                )

        queued_locations = {token_id: queued_ids.count(token_id) for token_id in queued_ids}
        for token in self.tokens:
            location_count = (
                queued_locations.get(token.token_id, 0)
                + dispatch_locations.get(token.token_id, 0)
                + waiting_locations.get(token.token_id, 0)
            )
            expected_count = 0 if token.scheduling_state is SchedulingState.SETTLED else 1
            if location_count != expected_count:
                if token.scheduling_state is SchedulingState.JOIN_WAITING:
                    raise ValueError(
                        "join-waiting token must occur in exactly one unsettled join obligation"
                    )
                raise ValueError(
                    "every live token must occur in exactly one matching scheduler location"
                )
            expected_location = {
                SchedulingState.QUEUED: queued_locations.get(token.token_id, 0),
                SchedulingState.EXECUTING: dispatch_locations.get(token.token_id, 0),
                SchedulingState.JOIN_WAITING: waiting_locations.get(token.token_id, 0),
                SchedulingState.SETTLED: 0,
            }[token.scheduling_state]
            if expected_location != expected_count:
                raise ValueError("token occurs in a scheduler location that contradicts its state")

        for token in self.tokens:
            parent_ids = (
                (token.parent_token_id,) if token.parent_token_id is not None else ()
            ) + token.continuation_parent_token_ids
            if any(parent_id not in tokens for parent_id in parent_ids):
                raise ValueError("token references a missing parent token")
            for lineage_index, frame in enumerate(token.fork_lineage):
                fork = forks.get(frame.fork_id)
                if fork is None:
                    raise ValueError("token fork lineage references a missing fork owner")
                if fork.parent_fork_id != frame.parent_fork_id:
                    raise ValueError("token fork lineage contradicts its durable fork owner")
                if lineage_index == len(token.fork_lineage) - 1 and token.token_id not in {
                    child.token_id for child in fork.children
                }:
                    retired_nested_parent = (
                        token.scheduling_state is SchedulingState.SETTLED
                        and any(
                            nested.parent_fork_id == fork.fork_id
                            and nested.parent_token_id == token.token_id
                            for nested in self.forks
                        )
                    )
                    loop_descendant = any(
                        (
                            nested.enclosing_owner.token_id == token.parent_token_id
                            or (
                                nested.enclosing_owner.token_id in tokens
                                and any(
                                    lineage.fork_id == frame.fork_id
                                    and lineage.child_ordinal == frame.child_ordinal
                                    for lineage in tokens[
                                        nested.enclosing_owner.token_id
                                    ].fork_lineage
                                )
                            )
                        )
                        and any(
                            member.token_id == token.token_id
                            for nested_frame in nested.frames
                            for member in nested_frame.members
                        )
                        for nested in self.loops
                    )
                    retired_loop_owner = token.scheduling_state is SchedulingState.SETTLED and any(
                        loop.enclosing_owner.token_id == token.token_id
                        and any(
                            continuation_id in tokens
                            and any(
                                lineage.fork_id == frame.fork_id
                                and lineage.child_ordinal == frame.child_ordinal
                                for lineage in tokens[continuation_id].fork_lineage
                            )
                            for continuation_id in loop.emitted_continuation_token_ids
                        )
                        for loop in self.loops
                    )
                    if not retired_nested_parent and not loop_descendant and not retired_loop_owner:
                        raise ValueError("token is not a child of its innermost fork owner")
            for membership in token.iteration_memberships:
                loop = loops.get(membership.loop_instance_id)
                frame_pair = frames.get(membership.iteration_frame_id)
                if loop is None or frame_pair is None or frame_pair[0] is not loop:
                    raise ValueError("token iteration membership references a missing loop/frame")
                frame = frame_pair[1]
                member = next(
                    (item for item in frame.members if item.token_id == token.token_id), None
                )
                if member is None:
                    raise ValueError("token iteration membership has no matching frame member")
                if membership.iteration_index != frame.iteration_index:
                    raise ValueError("token iteration membership uses the wrong frame index")
                is_live_member = member.state is IterationMemberState.ACTIVE
                owns_unresolved_nested_scope = any(
                    nested.lifecycle_state.value != "completed"
                    and (
                        nested.enclosing_owner.token_id == token.token_id
                        or any(
                            member.token_id == token.token_id
                            for nested_frame in nested.frames
                            for member in nested_frame.members
                        )
                    )
                    for nested in self.loops
                    if nested.enclosing_owner.enclosing_loop_instance_id
                    == membership.loop_instance_id
                )
                token_is_live = token.scheduling_state is not SchedulingState.SETTLED
                if (is_live_member and not token_is_live and not owns_unresolved_nested_scope) or (
                    not is_live_member and token_is_live
                ):
                    raise ValueError("token settlement contradicts its iteration member state")

        token_parent_map = {
            token.token_id: (
                ((token.parent_token_id,) if token.parent_token_id is not None else ())
                + token.continuation_parent_token_ids
            )
            for token in self.tokens
        }
        if _has_cycle(token_parent_map):
            raise ValueError("token parent ownership contains a cycle")

        for fork in self.forks:
            if fork.parent_token_id not in tokens:
                raise ValueError("fork references a missing parent token")
            if tokens[fork.parent_token_id].scheduling_state is not SchedulingState.SETTLED:
                raise ValueError("fork child creation requires a settled parent token")
            if fork.parent_fork_id is not None and fork.parent_fork_id not in forks:
                raise ValueError("fork references a missing parent fork")
            if any(child.token_id not in tokens for child in fork.children):
                raise ValueError("fork references a missing child token")
            if any(
                obligation.outcome is not None
                and obligation.outcome is not ForkObligationOutcome.JOINED
                and tokens[obligation.child_token_id].scheduling_state
                is not SchedulingState.SETTLED
                and obligation.child_token_id not in waiting_locations
                for obligation in fork.obligations
            ):
                raise ValueError("a settled obligation source token must be durably SETTLED")
            for child in fork.children:
                token = tokens[child.token_id]
                transferred_continuation = any(
                    nested.parent_fork_id == fork.fork_id
                    and set(token.continuation_parent_token_ids)
                    == {nested_child.token_id for nested_child in nested.children}
                    and nested.parent_token_id in tokens
                    and tokens[nested.parent_token_id].parent_token_id == fork.parent_token_id
                    for nested in self.forks
                )
                transferred_loop_continuation = any(
                    token.token_id in loop.emitted_continuation_token_ids
                    and token.current_node_id == exit_state.target_node_id
                    and token.causal_inbound_edge_id == exit_state.exit_edge_id
                    and set(token.continuation_parent_token_ids)
                    == {
                        record.token_id
                        for record in exit_state.records
                        if record.outcome is LoopExitResolutionOutcome.DELIVERED
                        and record.surviving_fork_lineage
                        and record.surviving_fork_lineage[-1].fork_id == fork.fork_id
                        and record.surviving_fork_lineage[-1].child_ordinal
                        == child.creation_ordinal
                    }
                    for loop in self.loops
                    for exit_state in loop.exits
                )
                nested_loop_exit_continuation = any(
                    token.token_id in loop.emitted_continuation_token_ids
                    and token.current_node_id == exit_state.target_node_id
                    and token.causal_inbound_edge_id == exit_state.exit_edge_id
                    and fork.parent_fork_id is not None
                    and fork.parent_token_id == loop.enclosing_owner.token_id
                    and set(token.continuation_parent_token_ids)
                    == {
                        record.token_id
                        for record in exit_state.records
                        if record.outcome is LoopExitResolutionOutcome.DELIVERED
                    }
                    and all(
                        record.surviving_fork_lineage
                        and record.surviving_fork_lineage[-1].fork_id == fork.parent_fork_id
                        for record in exit_state.records
                        if record.outcome is LoopExitResolutionOutcome.DELIVERED
                    )
                    for loop in self.loops
                    for exit_state in loop.exits
                )
                if (
                    token.parent_token_id != fork.parent_token_id
                    and not transferred_continuation
                    and not transferred_loop_continuation
                    and not nested_loop_exit_continuation
                ):
                    raise ValueError(
                        "fork child immediate parent must match the ForkInstance parent token"
                    )
                matching_lineage = tuple(
                    frame
                    for frame in token.fork_lineage
                    if frame.fork_id == fork.fork_id
                    and frame.child_ordinal == child.creation_ordinal
                )
                if len(matching_lineage) != 1:
                    raise ValueError("fork child must retain exactly one matching fork lineage")
            if any(
                obligation.join_instance_id is not None
                and obligation.join_instance_id
                not in {join.join_instance_id for join in self.joins}
                for obligation in fork.obligations
            ):
                raise ValueError("fork obligation references a missing join instance")
        if _has_cycle(
            {
                item.fork_id: (() if item.parent_fork_id is None else (item.parent_fork_id,))
                for item in self.forks
            }
        ):
            raise ValueError("fork ownership contains a cycle")

        for loop in self.loops:
            owner = loop.enclosing_owner
            if owner.token_id not in tokens:
                raise ValueError("loop references a missing enclosing owner token")
            if owner.enclosing_loop_instance_id is not None:
                outer = loops.get(owner.enclosing_loop_instance_id)
                frame_pair = frames.get(owner.iteration_frame_id or "")
                if outer is None or frame_pair is None or frame_pair[0] is not outer:
                    raise ValueError(
                        "nested loop enclosing owner does not resolve to an outer frame"
                    )
                if owner.token_id not in {member.token_id for member in frame_pair[1].members}:
                    raise ValueError("nested loop owner token is not a member of its outer frame")
            if any(token_id not in tokens for token_id in loop.live_child_token_ids):
                raise ValueError("loop references a missing live child token")
            for frame in loop.frames:
                for member in frame.members:
                    token = tokens.get(member.token_id)
                    if token is None:
                        raise ValueError("iteration frame references a missing member token")
                    matching_memberships = tuple(
                        membership
                        for membership in token.iteration_memberships
                        if membership.loop_instance_id == loop.loop_instance_id
                        and membership.iteration_frame_id == frame.iteration_frame_id
                        and membership.iteration_index == frame.iteration_index
                    )
                    if len(matching_memberships) != 1:
                        raise ValueError(
                            "iteration frame member must retain exactly one matching iteration "
                            "membership"
                        )
            if any(token_id not in tokens for token_id in loop.emitted_continuation_token_ids):
                raise ValueError("loop references a missing emitted continuation token")
        if _has_cycle(
            {
                loop.loop_instance_id: (
                    ()
                    if loop.enclosing_owner.enclosing_loop_instance_id is None
                    else (loop.enclosing_owner.enclosing_loop_instance_id,)
                )
                for loop in self.loops
            }
        ):
            raise ValueError("loop ownership contains a cycle")

        component_revisions = [token.state_revision for token in self.tokens]
        component_revisions.extend(item.updated_revision for item in self.forks)
        component_revisions.extend(item.updated_revision for item in self.joins)
        component_revisions.extend(item.updated_revision for item in self.loops)
        component_revisions.extend(item.updated_revision for item in self.in_flight_dispatches)
        if self.cancellation_fence is not None:
            component_revisions.append(self.cancellation_fence.state_revision)
        if any(revision > self.revision for revision in component_revisions):
            raise ValueError("component state cannot be newer than the snapshot revision")

        if self.state is TokenEngineSnapshotState.CANCELLED and (
            self.cancellation_fence is None or self.cancellation_fence.generation == 0
        ):
            raise ValueError("a CANCELLED snapshot requires a positive cancellation fence")

        return self


__all__ = [
    "SnapshotSchemaVersion",
    "TokenEngineSnapshot",
    "TokenEngineSnapshotState",
    "TokenOrdinal",
]
