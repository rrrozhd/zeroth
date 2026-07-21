"""Reducer claim, close, and ownership transitions for token joins."""

from __future__ import annotations

import asyncio
import uuid
from typing import cast

from pydantic import JsonValue

from zeroth.contracts.graph.models import JoinConfig
from zeroth.contracts.graph.token_snapshot import TokenEngineSnapshot
from zeroth.contracts.graph.tokens import (
    ForkChild,
    ForkInstance,
    ForkLifecycleState,
    ForkObligation,
    IterationMemberState,
    JoinInstance,
    JoinLifecycleState,
    JoinObligationOutcome,
    SchedulingState,
    TokenEnvelope,
    TokenLifecycleState,
)
from zeroth.runtime.orchestration.token_join_helpers import (
    replace_fork as _replace_fork,
)
from zeroth.runtime.orchestration.token_join_helpers import (
    replace_join as _replace_join,
)
from zeroth.runtime.orchestration.token_join_models import (
    FailureMode,
    JoinReducer,
    JoinReducerInput,
    TokenJoinTransitionError,
)
from zeroth.runtime.orchestration.token_join_reducers import (
    _config_fingerprint,
    _join_inputs,
    _json_value,
    reduce_join_inputs,
)
from zeroth.runtime.orchestration.token_scheduler import (
    TokenSchedulerTransitionError,
    _model_data,
    _next_snapshot,
    _replace_token,
    _stable_id,
    _update_iteration_ownership,
    _updated_token,
    apply_token_transition,
)
from zeroth.runtime.orchestration.token_snapshot_store import (
    TokenSnapshotConcurrencyError,
    TokenSnapshotStore,
)


def _transfer_outer_fork(
    forks: tuple[ForkInstance, ...],
    inner: ForkInstance,
    continuation: TokenEnvelope,
    revision: int,
) -> tuple[ForkInstance, ...]:
    if inner.parent_fork_id is None:
        return forks
    outer = next((fork for fork in forks if fork.fork_id == inner.parent_fork_id), None)
    if outer is None:
        raise TokenJoinTransitionError("nested join has a missing ancestor fork")
    if outer.lifecycle_state is ForkLifecycleState.CLOSED:
        raise TokenJoinTransitionError("nested join cannot resume a closed ancestor obligation")
    matched = False
    children: list[ForkChild] = []
    obligations: list[ForkObligation] = []
    for child in outer.children:
        if child.token_id == inner.parent_token_id:
            matched = True
            children.append(
                ForkChild(token_id=continuation.token_id, creation_ordinal=child.creation_ordinal)
            )
        else:
            children.append(child)
    for obligation in outer.obligations:
        if obligation.child_token_id == inner.parent_token_id:
            if obligation.outcome is not None:
                raise TokenJoinTransitionError("ancestor obligation was already settled")
            obligations.append(
                ForkObligation.model_validate(
                    {**_model_data(obligation), "child_token_id": continuation.token_id}
                )
            )
        else:
            obligations.append(obligation)
    if not matched:
        raise TokenJoinTransitionError("nested join has no ancestor child slot to resume")
    replacement = ForkInstance.model_validate(
        {
            **_model_data(outer),
            "children": tuple(children),
            "obligations": tuple(obligations),
            "updated_revision": revision,
        }
    )
    return _replace_fork(forks, replacement)


def close_ready_join(
    snapshot: TokenEngineSnapshot,
    join_instance_id: str,
    config: JoinConfig,
    *,
    reducer: JoinReducer = reduce_join_inputs,
    failure_mode: FailureMode | None = None,
    reduction_claim_id: str | None = None,
) -> TokenEngineSnapshot:
    """Consume one READY cohort and publish at most one continuation revision."""
    join = next(
        (item for item in snapshot.joins if item.join_instance_id == join_instance_id), None
    )
    if join is None:
        raise TokenJoinTransitionError(f"join {join_instance_id!r} does not exist")
    declared_failure_mode = join.failure_mode
    if failure_mode is not None and failure_mode != declared_failure_mode:
        raise TokenJoinTransitionError("close failure policy contradicts persisted join")
    fingerprint = _config_fingerprint(config)
    if join.lifecycle_state is JoinLifecycleState.CLOSED:
        if join.reducer_fingerprint is not None and join.reducer_fingerprint != fingerprint:
            raise TokenJoinTransitionError("replayed close contradicts persisted reducer config")
        return snapshot
    if join.lifecycle_state is JoinLifecycleState.REDUCING:
        if reduction_claim_id != join.reduction_claim_id:
            raise TokenJoinTransitionError("join reduction claim is owned by another closer")
        if join.reducer_fingerprint != fingerprint:
            raise TokenJoinTransitionError("join reduction config contradicts persisted claim")
    elif join.lifecycle_state is not JoinLifecycleState.READY:
        raise TokenJoinTransitionError("only a READY/REDUCING join may be closed")
    outcomes = {item.outcome for item in join.obligations}
    if declared_failure_mode == "fail_fast" and outcomes & {
        JoinObligationOutcome.FAILED,
        JoinObligationOutcome.CANCELLED,
    }:
        raise TokenJoinTransitionError("fail-fast join cannot continue after failure/cancellation")

    inputs = _join_inputs(snapshot, join)
    reduced = _json_value(reducer(config, inputs))
    revision = snapshot.revision + 1
    consumed_ids = tuple(item.source_token_id for item in join.obligations)
    sources = [
        next(token for token in snapshot.tokens if token.token_id == token_id)
        for token_id in consumed_ids
    ]
    first = sources[0]
    inner_fork = next(fork for fork in snapshot.forks if fork.fork_id == join.fork_id)
    continuation_id = _stable_id("tok", snapshot.run_id, f"join:{join.join_instance_id}", 0)
    continuation = TokenEnvelope(
        token_id=continuation_id,
        continuation_parent_token_ids=consumed_ids,
        provenance_tag=join.provenance_tag,
        current_node_id=join.target_node_id,
        causal_inbound_edge_id=None,
        payload=reduced,
        lifecycle_state=TokenLifecycleState.ACTIVE,
        scheduling_state=SchedulingState.QUEUED,
        fork_lineage=first.fork_lineage[:-1],
        iteration_memberships=first.iteration_memberships,
        cancellation_generation=first.cancellation_generation,
        state_revision=revision,
    )
    tokens = snapshot.tokens
    for source in sources:
        if source.scheduling_state is not SchedulingState.JOIN_WAITING:
            raise TokenJoinTransitionError("READY join source is not durably JOIN_WAITING")
        tokens = _replace_token(
            tokens,
            _updated_token(
                source,
                lifecycle_state=TokenLifecycleState.SETTLED,
                scheduling_state=SchedulingState.SETTLED,
                state_revision=revision,
                settled_revision=revision,
            ),
        )
    tokens = (*tokens, continuation)
    updated_join = JoinInstance.model_validate(
        {
            **_model_data(join),
            "lifecycle_state": JoinLifecycleState.CLOSED,
            "continuation_token_id": continuation_id,
            "consumed_parent_token_ids": consumed_ids,
            "reducer_fingerprint": fingerprint,
            "reduction_claim_id": None,
            "updated_revision": revision,
            "closed_revision": revision,
        }
    )
    forks = _transfer_outer_fork(snapshot.forks, inner_fork, continuation, revision)
    loops = snapshot.loops
    outcomes_by_token = {item.source_token_id: item.outcome for item in join.obligations}
    for index, source in enumerate(sources):
        if not source.iteration_memberships:
            continue
        loop_snapshot = snapshot.model_copy(update={"loops": loops})
        member_state = {
            JoinObligationOutcome.SUPPRESSED: IterationMemberState.SUPPRESSED,
            JoinObligationOutcome.FAILED: IterationMemberState.FAILED,
            JoinObligationOutcome.CANCELLED: IterationMemberState.CANCELLED,
        }.get(
            outcomes_by_token[source.token_id],
            IterationMemberState.INTERNAL_COMPLETION,
        )
        try:
            loops = _update_iteration_ownership(
                loop_snapshot,
                source,
                revision=revision,
                parent_state=member_state,
                child_token_ids=((continuation.token_id,) if index == 0 else ()),
            )
        except TokenSchedulerTransitionError as exc:
            raise TokenJoinTransitionError(str(exc)) from exc
    return _next_snapshot(
        snapshot,
        next_token_ordinal=snapshot.next_token_ordinal + 1,
        queue=(*snapshot.queue, continuation),
        tokens=tokens,
        forks=forks,
        joins=_replace_join(snapshot.joins, updated_join),
        loops=loops,
    )


async def close_ready_join_with_cas(
    store: TokenSnapshotStore,
    run_id: str,
    join_instance_id: str,
    config: JoinConfig,
    *,
    reducer: JoinReducer = reduce_join_inputs,
    failure_mode: FailureMode | None = None,
    max_attempts: int = 8,
) -> TokenEngineSnapshot:
    """Claim reduction through CAS, then evaluate one deterministic reducer.

    The durable claim makes concurrent losers wait/reload without invoking the
    reducer. A process crash after claiming leaves an explicit REDUCING record;
    recovery of that ambiguity is intentionally a later lifecycle operation.
    Reducers must remain deterministic and side-effect free.
    """
    claim_id = uuid.uuid4().hex
    fingerprint = _config_fingerprint(config)
    claimed: TokenEngineSnapshot | None = None
    last_error: TokenSnapshotConcurrencyError | None = None
    for _ in range(max_attempts):
        current = await store.get_token_snapshot(run_id)
        if current is None:
            raise TokenJoinTransitionError(f"run {run_id!r} has no token snapshot")
        join = next(
            (item for item in current.joins if item.join_instance_id == join_instance_id), None
        )
        if join is None:
            raise TokenJoinTransitionError(f"join {join_instance_id!r} does not exist")
        if join.lifecycle_state is JoinLifecycleState.CLOSED:
            return close_ready_join(
                current,
                join_instance_id,
                config,
                failure_mode=failure_mode,
            )
        if join.lifecycle_state is JoinLifecycleState.REDUCING:
            if join.reduction_claim_id == claim_id:
                claimed = current
                break
            await asyncio.sleep(0)
            continue
        if join.lifecycle_state is not JoinLifecycleState.READY:
            raise TokenJoinTransitionError("only a READY join may claim reduction")
        if failure_mode is not None and failure_mode != join.failure_mode:
            raise TokenJoinTransitionError("close failure policy contradicts persisted join")
        revision = current.revision + 1
        claimed_join = JoinInstance.model_validate(
            {
                **_model_data(join),
                "lifecycle_state": JoinLifecycleState.REDUCING,
                "reducer_fingerprint": fingerprint,
                "reduction_claim_id": claim_id,
                "updated_revision": revision,
            }
        )
        proposed = _next_snapshot(
            current,
            joins=_replace_join(current.joins, claimed_join),
        )
        try:
            claimed = await store.compare_and_swap_token_snapshot(
                run_id,
                expected_revision=current.revision,
                snapshot=proposed,
            )
        except TokenSnapshotConcurrencyError as exc:
            last_error = exc
            continue
        break
    if claimed is None:
        if last_error is not None:
            raise last_error
        raise TokenJoinTransitionError("join reduction is claimed by another live closer")

    claimed_join = next(item for item in claimed.joins if item.join_instance_id == join_instance_id)
    reduced = _json_value(reducer(config, _join_inputs(claimed, claimed_join)))

    def prepared_reducer(_config: JoinConfig, _inputs: tuple[JoinReducerInput, ...]) -> JsonValue:
        return reduced

    return await apply_token_transition(
        store,
        run_id,
        lambda current: close_ready_join(
            cast(TokenEngineSnapshot, current),
            join_instance_id,
            config,
            reducer=prepared_reducer,
            failure_mode=failure_mode,
            reduction_claim_id=claim_id,
        ),
        max_attempts=max_attempts,
    )


__all__ = ["close_ready_join", "close_ready_join_with_cas"]
