"""Internal identity and immutable-replacement helpers for token joins."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from zeroth.contracts.graph.token_snapshot import TokenEngineSnapshot
from zeroth.contracts.graph.tokens import (
    ForkInstance,
    ForkLifecycleState,
    ForkObligationOutcome,
    JoinInstance,
    JoinObligationOutcome,
    PayloadDelivery,
    SchedulingState,
    TokenEnvelope,
)
from zeroth.runtime.orchestration.token_join_models import (
    FailureMode,
    TokenJoinTransitionError,
)
from zeroth.runtime.orchestration.token_scheduler import (
    TokenSchedulerTransitionError,
    _matching_dispatch,
    _stable_id,
)


def join_id(
    snapshot: TokenEngineSnapshot,
    fork: ForkInstance,
    target_node_id: str,
    token: TokenEnvelope,
) -> str:
    provenance = json.dumps(
        [frame.model_dump(mode="json") for frame in token.provenance_tag],
        sort_keys=True,
        separators=(",", ":"),
    )
    material = f"{fork.fork_id}\0{target_node_id}\0{provenance}".encode()
    ordinal = int(hashlib.sha256(material).hexdigest()[:12], 16)
    return _stable_id("join", snapshot.run_id, fork.fork_id, ordinal)


def arrival_command_fingerprint(
    *,
    token_id: str | None,
    dispatch_id: str | None,
    attempt: int | None,
    cancellation_generation: int | None,
    target_node_id: str,
    inbound_edge_id: str,
    cohort_inbound_edges: Mapping[str, str],
    outcome: JoinObligationOutcome,
    delivery: PayloadDelivery | None,
    failure_mode: FailureMode,
) -> str:
    """Return a stable identity for every semantic field in an arrival command."""
    command = {
        "source": (
            {"kind": "dispatch", "id": dispatch_id}
            if dispatch_id is not None
            else {"kind": "token", "id": token_id}
        ),
        "attempt": attempt,
        "cancellation_generation": cancellation_generation,
        "target_node_id": target_node_id,
        "inbound_edge_id": inbound_edge_id,
        "cohort_inbound_edges": sorted(cohort_inbound_edges.items()),
        "outcome": outcome.value,
        "delivery": None if delivery is None else delivery.model_dump(mode="json"),
        "failure_mode": failure_mode,
    }
    material = json.dumps(command, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(material).hexdigest()


def replace_fork(
    forks: tuple[ForkInstance, ...], replacement: ForkInstance
) -> tuple[ForkInstance, ...]:
    return tuple(replacement if fork.fork_id == replacement.fork_id else fork for fork in forks)


def replace_join(
    joins: tuple[JoinInstance, ...], replacement: JoinInstance
) -> tuple[JoinInstance, ...]:
    return tuple(
        replacement if join.join_instance_id == replacement.join_instance_id else join
        for join in joins
    )


def fork_for_token(snapshot: TokenEngineSnapshot, token: TokenEnvelope) -> ForkInstance:
    if not token.fork_lineage:
        raise TokenJoinTransitionError("a join arrival requires a fork-owned token")
    fork_id = token.fork_lineage[-1].fork_id
    fork = next((item for item in snapshot.forks if item.fork_id == fork_id), None)
    if fork is None:
        raise TokenJoinTransitionError("join arrival has a missing fork cohort")
    if fork.lifecycle_state is ForkLifecycleState.CLOSED:
        reserved = token.scheduling_state is SchedulingState.JOIN_WAITING and any(
            obligation.child_token_id == token.token_id
            and obligation.outcome is ForkObligationOutcome.JOINED
            for obligation in fork.obligations
        )
        if not reserved:
            raise TokenJoinTransitionError("join arrival belongs to an already-closed fork cohort")
    return fork


def source_token(
    snapshot: TokenEngineSnapshot,
    *,
    token_id: str | None,
    dispatch_id: str | None,
    attempt: int | None,
    cancellation_generation: int | None,
) -> tuple[TokenEnvelope, str | None]:
    if dispatch_id is not None:
        if token_id is not None:
            raise TokenJoinTransitionError("name either token_id or dispatch_id, not both")
        if attempt is None or cancellation_generation is None:
            raise TokenJoinTransitionError("dispatch arrivals require attempt and generation")
        try:
            dispatch = _matching_dispatch(
                snapshot,
                dispatch_id=dispatch_id,
                attempt=attempt,
                cancellation_generation=cancellation_generation,
            )
        except TokenSchedulerTransitionError as exc:
            raise TokenJoinTransitionError(str(exc)) from exc
        return dispatch.token, dispatch_id
    if token_id is None:
        raise TokenJoinTransitionError("a join arrival requires token_id or dispatch_id")
    token = next((item for item in snapshot.tokens if item.token_id == token_id), None)
    if token is None:
        raise TokenJoinTransitionError(f"join source token {token_id!r} does not exist")
    if token.scheduling_state not in {SchedulingState.QUEUED, SchedulingState.JOIN_WAITING}:
        raise TokenJoinTransitionError("direct join arrivals require a queued token")
    fence_generation = (
        0 if snapshot.cancellation_fence is None else snapshot.cancellation_fence.generation
    )
    if token.cancellation_generation != fence_generation:
        raise TokenJoinTransitionError("join arrival cancellation generation is stale")
    return token, None


def canonical_routes(
    fork: ForkInstance, routes: Mapping[str, str]
) -> tuple[tuple[str, str, int], ...]:
    expected = {child.token_id for child in fork.children}
    if set(routes) != expected or any(not edge_id for edge_id in routes.values()):
        raise TokenJoinTransitionError(
            "cohort_inbound_edges must exactly register every fork child"
        )
    return tuple(
        (child.token_id, routes[child.token_id], child.creation_ordinal) for child in fork.children
    )


def mapped_fork_outcome(outcome: JoinObligationOutcome) -> ForkObligationOutcome:
    return {
        JoinObligationOutcome.DELIVERED: ForkObligationOutcome.JOINED,
        JoinObligationOutcome.SUPPRESSED: ForkObligationOutcome.SUPPRESSED,
        JoinObligationOutcome.FAILED: ForkObligationOutcome.FAILED,
        JoinObligationOutcome.CANCELLED: ForkObligationOutcome.CANCELLED,
    }[outcome]


__all__ = [
    "arrival_command_fingerprint",
    "canonical_routes",
    "fork_for_token",
    "join_id",
    "mapped_fork_outcome",
    "replace_fork",
    "replace_join",
    "source_token",
]
