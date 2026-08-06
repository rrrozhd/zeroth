"""Arrival, obligation, and fail-fast transitions for token joins."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import JsonValue

from zeroth.contracts.graph.token_snapshot import TokenEngineSnapshot
from zeroth.contracts.graph.tokens import (
    ForkInstance,
    ForkLifecycleState,
    ForkObligation,
    ForkObligationOutcome,
    IterationMemberState,
    JoinInstance,
    JoinLifecycleState,
    JoinObligation,
    JoinObligationOutcome,
    PayloadDelivery,
    SchedulingState,
    TokenLifecycleState,
)
from zeroth.runtime.orchestration.token_join_helpers import (
    arrival_command_fingerprint as _arrival_command_fingerprint,
)
from zeroth.runtime.orchestration.token_join_helpers import (
    canonical_routes as _canonical_routes,
)
from zeroth.runtime.orchestration.token_join_helpers import (
    fork_for_token as _fork_for_token,
)
from zeroth.runtime.orchestration.token_join_helpers import (
    join_id as _join_id,
)
from zeroth.runtime.orchestration.token_join_helpers import (
    mapped_fork_outcome as _mapped_fork_outcome,
)
from zeroth.runtime.orchestration.token_join_helpers import (
    replace_fork as _replace_fork,
)
from zeroth.runtime.orchestration.token_join_helpers import (
    replace_join as _replace_join,
)
from zeroth.runtime.orchestration.token_join_helpers import (
    source_token as _source_token,
)
from zeroth.runtime.orchestration.token_join_models import (
    FailureMode,
    TokenJoinTransitionError,
)
from zeroth.runtime.orchestration.token_scheduler import (
    TokenSchedulerTransitionError,
    _model_data,
    _next_snapshot,
    _replace_token,
    _settle_innermost_fork,
    _stable_id,
    _update_iteration_ownership,
    _updated_token,
)


def _settle_arrival(
    snapshot: TokenEngineSnapshot,
    *,
    target_node_id: str,
    inbound_edge_id: str,
    cohort_inbound_edges: Mapping[str, str],
    outcome: JoinObligationOutcome,
    delivery: PayloadDelivery | None,
    token_id: str | None = None,
    dispatch_id: str | None = None,
    attempt: int | None = None,
    cancellation_generation: int | None = None,
    failure_mode: FailureMode = "fail_fast",
) -> TokenEngineSnapshot:
    reported_outcome = outcome
    reported_delivery = delivery
    command_fingerprint = _arrival_command_fingerprint(
        token_id=token_id,
        dispatch_id=dispatch_id,
        attempt=attempt,
        cancellation_generation=cancellation_generation,
        target_node_id=target_node_id,
        inbound_edge_id=inbound_edge_id,
        cohort_inbound_edges=cohort_inbound_edges,
        outcome=reported_outcome,
        delivery=reported_delivery,
        failure_mode=failure_mode,
    )
    if token_id is not None and dispatch_id is not None:
        raise TokenJoinTransitionError("name either token_id or dispatch_id, not both")
    if token_id is None and dispatch_id is None:
        raise TokenJoinTransitionError("a join arrival requires token_id or dispatch_id")
    # A dispatch is removed by the winning arrival revision.  Replays therefore
    # resolve by the durable target/edge resolution before trying to re-read the
    # no-longer-live dispatch record.
    if dispatch_id is not None and not any(
        item.dispatch_id == dispatch_id for item in snapshot.in_flight_dispatches
    ):
        persisted = [
            obligation
            for join in snapshot.joins
            for obligation in join.obligations
            if obligation.source_dispatch_id == dispatch_id
        ]
        if len(persisted) == 1:
            obligation = persisted[0]
            if obligation.arrival_command_fingerprint == command_fingerprint:
                return snapshot
            raise TokenJoinTransitionError("join arrival replay contradicts persisted resolution")
    if token_id is not None:
        persisted = [
            obligation
            for join in snapshot.joins
            for obligation in join.obligations
            if obligation.source_token_id == token_id
        ]
        if len(persisted) > 1:
            raise TokenJoinTransitionError("join source has ambiguous persisted resolutions")
        if persisted:
            obligation = persisted[0]
            if obligation.arrival_command_fingerprint == command_fingerprint:
                return snapshot
            if obligation.outcome is not None:
                raise TokenJoinTransitionError(
                    "join arrival replay contradicts persisted resolution"
                )
    token, consumed_dispatch_id = _source_token(
        snapshot,
        token_id=token_id,
        dispatch_id=dispatch_id,
        attempt=attempt,
        cancellation_generation=cancellation_generation,
    )
    fork = _fork_for_token(snapshot, token)
    routes = _canonical_routes(fork, cohort_inbound_edges)
    tokens_by_id = {item.token_id: item for item in snapshot.tokens}
    if any(
        tokens_by_id[source_id].provenance_tag != token.provenance_tag
        or tokens_by_id[source_id].iteration_memberships != token.iteration_memberships
        for source_id, _, _ in routes
    ):
        raise TokenJoinTransitionError("join fork cohort crosses iteration scope")
    if cohort_inbound_edges[token.token_id] != inbound_edge_id:
        raise TokenJoinTransitionError("arrival edge contradicts its registered cohort route")
    join_id = _join_id(snapshot, fork, target_node_id, token)
    join = next((item for item in snapshot.joins if item.join_instance_id == join_id), None)
    if join is not None and (
        join.fork_id != fork.fork_id
        or join.target_node_id != target_node_id
        or join.provenance_tag != token.provenance_tag
        or join.iteration_memberships != token.iteration_memberships
    ):
        raise TokenJoinTransitionError("deterministic join identity contradicts persisted cohort")
    if join is not None and join.failure_mode != failure_mode:
        raise TokenJoinTransitionError("arrival failure policy contradicts persisted join")
    if (
        join is not None
        and join.failure_mode == "fail_fast"
        and any(
            item.outcome in {JoinObligationOutcome.FAILED, JoinObligationOutcome.CANCELLED}
            for item in join.obligations
        )
    ):
        # Cooperative cohort-scoped cancellation: an already-running sibling
        # may finish normally, but its result is discarded when it reports.
        outcome = JoinObligationOutcome.CANCELLED
        delivery = None

    revision = snapshot.revision + 1
    if join is None:
        fork_outcomes = {item.child_token_id: item for item in fork.obligations}
        inherited_outcomes = {
            ForkObligationOutcome.SUPPRESSED: JoinObligationOutcome.SUPPRESSED,
            ForkObligationOutcome.FAILED: JoinObligationOutcome.FAILED,
            ForkObligationOutcome.CANCELLED: JoinObligationOutcome.CANCELLED,
        }
        obligations = [
            JoinObligation(
                obligation_id=_stable_id("jobl", snapshot.run_id, join_id, ordinal),
                join_instance_id=join_id,
                fork_id=fork.fork_id,
                source_token_id=source_id,
                inbound_edge_id=edge_id,
                child_ordinal=ordinal,
                outcome=inherited_outcomes.get(fork_outcomes[source_id].outcome),
                settled_revision=(
                    revision if fork_outcomes[source_id].outcome in inherited_outcomes else None
                ),
            )
            for source_id, edge_id, ordinal in routes
        ]
        if failure_mode == "fail_fast" and any(
            item.outcome
            in {
                JoinObligationOutcome.FAILED,
                JoinObligationOutcome.CANCELLED,
            }
            for item in obligations
        ):
            outcome = JoinObligationOutcome.CANCELLED
            delivery = None
        created_revision = revision
    else:
        registered = tuple(
            (item.source_token_id, item.inbound_edge_id, item.child_ordinal)
            for item in join.obligations
        )
        if registered != routes:
            raise TokenJoinTransitionError("arrival cohort routes contradict persisted join")
        obligations = list(join.obligations)
        created_revision = join.created_revision

    matched = False
    updated_obligations: list[JoinObligation] = []
    for obligation in obligations:
        if obligation.source_token_id != token.token_id:
            updated_obligations.append(obligation)
            continue
        if obligation.outcome is not None:
            raise TokenJoinTransitionError("join obligation is already resolved")
        matched = True
        data = _model_data(obligation)
        data.update(
            outcome=outcome,
            delivery=delivery,
            source_dispatch_id=consumed_dispatch_id,
            source_dispatch_attempt=(attempt if consumed_dispatch_id is not None else None),
            source_cancellation_generation=(
                cancellation_generation if consumed_dispatch_id is not None else None
            ),
            source_reported_outcome=(
                reported_outcome if consumed_dispatch_id is not None else None
            ),
            source_reported_delivery=(
                reported_delivery if consumed_dispatch_id is not None else None
            ),
            arrival_command_fingerprint=command_fingerprint,
            settled_revision=revision,
        )
        updated_obligations.append(JoinObligation.model_validate(data))
    if not matched:
        raise TokenJoinTransitionError("join source is outside the registered fork cohort")

    fork_obligations: list[ForkObligation] = []
    for obligation in fork.obligations:
        if obligation.child_token_id != token.token_id:
            fork_obligations.append(obligation)
            continue
        data = _model_data(obligation)
        mapped = _mapped_fork_outcome(outcome)
        if obligation.outcome is not None:
            reserved = (
                obligation.outcome is ForkObligationOutcome.JOINED
                and obligation.join_instance_id == join_id
                and token.scheduling_state is SchedulingState.JOIN_WAITING
            )
            if not reserved:
                raise TokenJoinTransitionError("fork obligation is already settled")
            if mapped is ForkObligationOutcome.JOINED:
                fork_obligations.append(obligation)
                continue
        data.update(
            outcome=mapped,
            join_instance_id=(join_id if mapped is ForkObligationOutcome.JOINED else None),
            settled_revision=revision,
        )
        fork_obligations.append(ForkObligation.model_validate(data))

    auto_cancelled_ids: set[str] = set()
    if failure_mode == "fail_fast" and outcome in {
        JoinObligationOutcome.FAILED,
        JoinObligationOutcome.CANCELLED,
    }:
        for index, obligation in enumerate(updated_obligations):
            if obligation.outcome is not None:
                continue
            sibling = tokens_by_id[obligation.source_token_id]
            if sibling.scheduling_state is SchedulingState.QUEUED:
                auto_cancelled_ids.add(sibling.token_id)
                updated_obligations[index] = JoinObligation.model_validate(
                    {
                        **_model_data(obligation),
                        "outcome": JoinObligationOutcome.CANCELLED,
                        "settled_revision": revision,
                    }
                )
                for fork_index, fork_obligation in enumerate(fork_obligations):
                    if fork_obligation.child_token_id == sibling.token_id:
                        fork_obligations[fork_index] = ForkObligation.model_validate(
                            {
                                **_model_data(fork_obligation),
                                "outcome": ForkObligationOutcome.CANCELLED,
                                "settled_revision": revision,
                            }
                        )
                        break

    all_settled = all(item.outcome is not None for item in updated_obligations)
    any_delivered = any(
        item.outcome is JoinObligationOutcome.DELIVERED for item in updated_obligations
    )
    lifecycle = (
        JoinLifecycleState.READY
        if all_settled and any_delivered
        else JoinLifecycleState.CLOSED
        if all_settled
        else JoinLifecycleState.OPEN
    )
    consumed_ids = (
        tuple(item.source_token_id for item in updated_obligations)
        if lifecycle is JoinLifecycleState.CLOSED
        else ()
    )
    updated_join = JoinInstance(
        join_instance_id=join_id,
        fork_id=fork.fork_id,
        target_node_id=target_node_id,
        provenance_tag=token.provenance_tag,
        iteration_memberships=token.iteration_memberships,
        obligations=tuple(updated_obligations),
        failure_mode=failure_mode,
        lifecycle_state=lifecycle,
        consumed_parent_token_ids=consumed_ids,
        created_revision=created_revision,
        updated_revision=revision,
        closed_revision=(revision if lifecycle is JoinLifecycleState.CLOSED else None),
    )
    outstanding = sum(item.outcome is None for item in fork_obligations)
    updated_fork = ForkInstance.model_validate(
        {
            **_model_data(fork),
            "obligations": tuple(fork_obligations),
            "outstanding_child_count": outstanding,
            "lifecycle_state": (
                ForkLifecycleState.OPEN if outstanding else ForkLifecycleState.CLOSED
            ),
            "updated_revision": revision,
            "closed_revision": None if outstanding else revision,
        }
    )

    arrived = _updated_token(
        token,
        lifecycle_state=(
            TokenLifecycleState.SETTLED
            if lifecycle is JoinLifecycleState.CLOSED
            else TokenLifecycleState.ACTIVE
        ),
        scheduling_state=(
            SchedulingState.SETTLED
            if lifecycle is JoinLifecycleState.CLOSED
            else SchedulingState.JOIN_WAITING
        ),
        state_revision=revision,
        settled_revision=(revision if lifecycle is JoinLifecycleState.CLOSED else None),
    )
    tokens = _replace_token(snapshot.tokens, arrived)
    for existing in tuple(tokens):
        if existing.token_id in auto_cancelled_ids:
            tokens = _replace_token(
                tokens,
                _updated_token(
                    existing,
                    lifecycle_state=TokenLifecycleState.SETTLED,
                    scheduling_state=SchedulingState.SETTLED,
                    state_revision=revision,
                    settled_revision=revision,
                ),
            )
    joins = (
        (*snapshot.joins, updated_join)
        if join is None
        else _replace_join(snapshot.joins, updated_join)
    )
    if lifecycle is JoinLifecycleState.CLOSED:
        for source_id in consumed_ids:
            source = next(item for item in tokens if item.token_id == source_id)
            if source.scheduling_state is not SchedulingState.SETTLED:
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

    in_flight = tuple(
        item for item in snapshot.in_flight_dispatches if item.dispatch_id != consumed_dispatch_id
    )
    forks = _replace_fork(snapshot.forks, updated_fork)
    loops = snapshot.loops
    for source_id in auto_cancelled_ids:
        source = next(item for item in snapshot.tokens if item.token_id == source_id)
        if source.iteration_memberships:
            loop_snapshot = snapshot.model_copy(update={"loops": loops})
            try:
                loops = _update_iteration_ownership(
                    loop_snapshot,
                    source,
                    revision=revision,
                    parent_state=IterationMemberState.CANCELLED,
                )
            except TokenSchedulerTransitionError as exc:
                raise TokenJoinTransitionError(str(exc)) from exc
    if lifecycle is JoinLifecycleState.CLOSED:
        outcomes_by_token = {item.source_token_id: item.outcome for item in updated_obligations}
        for source_id in consumed_ids:
            if source_id in auto_cancelled_ids:
                continue
            source = next(item for item in snapshot.tokens if item.token_id == source_id)
            if source.iteration_memberships:
                loop_snapshot = snapshot.model_copy(update={"loops": loops})
                member_state = {
                    JoinObligationOutcome.SUPPRESSED: IterationMemberState.SUPPRESSED,
                    JoinObligationOutcome.FAILED: IterationMemberState.FAILED,
                    JoinObligationOutcome.CANCELLED: IterationMemberState.CANCELLED,
                }.get(
                    outcomes_by_token[source_id],
                    IterationMemberState.INTERNAL_COMPLETION,
                )
                try:
                    loops = _update_iteration_ownership(
                        loop_snapshot,
                        source,
                        revision=revision,
                        parent_state=member_state,
                    )
                except TokenSchedulerTransitionError as exc:
                    raise TokenJoinTransitionError(str(exc)) from exc
        if updated_fork.parent_fork_id is not None:
            parent = next(
                item for item in snapshot.tokens if item.token_id == updated_fork.parent_token_id
            )
            outcomes = {item.outcome for item in updated_obligations}
            aggregate = (
                ForkObligationOutcome.FAILED
                if JoinObligationOutcome.FAILED in outcomes
                else ForkObligationOutcome.CANCELLED
                if JoinObligationOutcome.CANCELLED in outcomes
                else ForkObligationOutcome.SUPPRESSED
            )
            propagated = _settle_innermost_fork(
                snapshot,
                parent,
                outcome=aggregate,
                revision=revision,
            )
            forks = _replace_fork(propagated, updated_fork)
            join_outcome = {
                ForkObligationOutcome.FAILED: JoinObligationOutcome.FAILED,
                ForkObligationOutcome.CANCELLED: JoinObligationOutcome.CANCELLED,
                ForkObligationOutcome.SUPPRESSED: JoinObligationOutcome.SUPPRESSED,
            }[aggregate]
            for outer in tuple(joins):
                if (
                    outer.fork_id != updated_fork.parent_fork_id
                    or outer.target_node_id != updated_join.target_node_id
                    or outer.lifecycle_state is not JoinLifecycleState.OPEN
                ):
                    continue
                outer_obligations: list[JoinObligation] = []
                matched_parent = False
                for outer_obligation in outer.obligations:
                    if outer_obligation.source_token_id != updated_fork.parent_token_id:
                        outer_obligations.append(outer_obligation)
                        continue
                    if outer_obligation.outcome is not None:
                        raise TokenJoinTransitionError(
                            "nested non-delivery cannot replace a resolved outer obligation"
                        )
                    matched_parent = True
                    outer_obligations.append(
                        JoinObligation.model_validate(
                            {
                                **_model_data(outer_obligation),
                                "outcome": join_outcome,
                                "delivery": None,
                                "settled_revision": revision,
                            }
                        )
                    )
                if not matched_parent:
                    continue
                outer_all_settled = all(item.outcome is not None for item in outer_obligations)
                outer_any_delivered = any(
                    item.outcome is JoinObligationOutcome.DELIVERED for item in outer_obligations
                )
                outer_lifecycle = (
                    JoinLifecycleState.READY
                    if outer_all_settled and outer_any_delivered
                    else JoinLifecycleState.CLOSED
                    if outer_all_settled
                    else JoinLifecycleState.OPEN
                )
                replacement = JoinInstance.model_validate(
                    {
                        **_model_data(outer),
                        "obligations": tuple(outer_obligations),
                        "lifecycle_state": outer_lifecycle,
                        "consumed_parent_token_ids": (
                            tuple(item.source_token_id for item in outer_obligations)
                            if outer_lifecycle is JoinLifecycleState.CLOSED
                            else ()
                        ),
                        "updated_revision": revision,
                        "closed_revision": (
                            revision if outer_lifecycle is JoinLifecycleState.CLOSED else None
                        ),
                    }
                )
                joins = _replace_join(joins, replacement)
    return _next_snapshot(
        snapshot,
        queue=tuple(
            item
            for item in snapshot.queue
            if item.token_id != token.token_id and item.token_id not in auto_cancelled_ids
        ),
        tokens=tokens,
        forks=forks,
        joins=joins,
        in_flight_dispatches=in_flight,
        loops=loops,
    )


def deliver_to_join(
    snapshot: TokenEngineSnapshot,
    *,
    target_node_id: str,
    inbound_edge_id: str,
    cohort_inbound_edges: Mapping[str, str],
    payload: JsonValue,
    token_id: str | None = None,
    dispatch_id: str | None = None,
    attempt: int | None = None,
    cancellation_generation: int | None = None,
    failure_mode: FailureMode = "fail_fast",
) -> TokenEngineSnapshot:
    """Register one delivered child resolution, preserving JSON null."""
    return _settle_arrival(
        snapshot,
        token_id=token_id,
        dispatch_id=dispatch_id,
        attempt=attempt,
        cancellation_generation=cancellation_generation,
        target_node_id=target_node_id,
        inbound_edge_id=inbound_edge_id,
        cohort_inbound_edges=cohort_inbound_edges,
        outcome=JoinObligationOutcome.DELIVERED,
        delivery=PayloadDelivery(payload=payload),
        failure_mode=failure_mode,
    )


def settle_join_without_delivery(
    snapshot: TokenEngineSnapshot,
    *,
    target_node_id: str,
    inbound_edge_id: str,
    cohort_inbound_edges: Mapping[str, str],
    outcome: JoinObligationOutcome,
    token_id: str | None = None,
    dispatch_id: str | None = None,
    attempt: int | None = None,
    cancellation_generation: int | None = None,
    failure_mode: FailureMode = "fail_fast",
) -> TokenEngineSnapshot:
    """Settle a suppressed, failed, or cancelled resolution without payload."""
    if outcome is JoinObligationOutcome.DELIVERED:
        raise TokenJoinTransitionError("non-delivery settlement cannot use DELIVERED")
    return _settle_arrival(
        snapshot,
        token_id=token_id,
        dispatch_id=dispatch_id,
        attempt=attempt,
        cancellation_generation=cancellation_generation,
        target_node_id=target_node_id,
        inbound_edge_id=inbound_edge_id,
        cohort_inbound_edges=cohort_inbound_edges,
        outcome=outcome,
        delivery=None,
        failure_mode=failure_mode,
    )


__all__ = [
    "deliver_to_join",
    "settle_join_without_delivery",
]
