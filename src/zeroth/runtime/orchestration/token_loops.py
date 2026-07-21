"""Small public facade for structured-token loop barriers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import JsonValue

from zeroth.contracts.graph.models import JoinConfig
from zeroth.contracts.graph.token_snapshot import TokenEngineSnapshot
from zeroth.contracts.graph.tokens import IterationMemberState
from zeroth.runtime.orchestration.token_join_reducers import reduce_join_inputs
from zeroth.runtime.orchestration.token_loop_claims import (
    close_ready_loop_with_cas as _close_ready_loop_with_cas,
)
from zeroth.runtime.orchestration.token_loop_claims import (
    reclaim_abandoned_loop_reduction_with_cas as _reclaim_abandoned_loop_reduction_with_cas,
)
from zeroth.runtime.orchestration.token_loop_closure import (
    close_ready_loop as _close_ready_loop,
)
from zeroth.runtime.orchestration.token_loop_models import (
    FailureMode,
    LoopReducer,
    LoopReductionClaim,
    LoopReductionClaimChangedError,
    LoopReductionRecoveryError,
    LoopReductionReleaseError,
    TokenLoopTransitionError,
)
from zeroth.runtime.orchestration.token_loop_transitions import enter_loop as _enter_loop
from zeroth.runtime.orchestration.token_loop_transitions import (
    settle_loop_member as _settle_loop_member,
)
from zeroth.runtime.orchestration.token_scheduler import FanOutBranch
from zeroth.runtime.orchestration.token_snapshot_store import TokenSnapshotStore


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
    """Retire a header token and atomically create iteration zero."""
    return _enter_loop(
        snapshot,
        token_id=token_id,
        dispatch_id=dispatch_id,
        attempt=attempt,
        cancellation_generation=cancellation_generation,
        loop_header_node_id=loop_header_node_id,
        body_node_id=body_node_id,
        inbound_edge_id=inbound_edge_id,
        exit_routes=exit_routes,
        body_payload=body_payload,
        body_branches=body_branches,
    )


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
    crossed_fork_ids: tuple[str, ...] | None = None,
    failure_mode: FailureMode = "fail_fast",
    allow_failure_suppression: bool = False,
) -> TokenEngineSnapshot:
    """Retire one member and settle every explicitly crossed scope."""
    return _settle_loop_member(
        snapshot,
        token_id=token_id,
        dispatch_id=dispatch_id,
        attempt=attempt,
        cancellation_generation=cancellation_generation,
        outcome=outcome,
        edge_id=edge_id,
        target_node_id=target_node_id,
        payload=payload,
        crossed_loop_instance_ids=crossed_loop_instance_ids,
        crossed_fork_ids=crossed_fork_ids,
        failure_mode=failure_mode,
        allow_failure_suppression=allow_failure_suppression,
    )


def close_ready_loop(
    snapshot: TokenEngineSnapshot,
    loop_instance_id: str,
    *,
    continuation_config: JoinConfig | None = None,
    reducer: LoopReducer = reduce_join_inputs,
    claimed_reduction: LoopReductionClaim | None = None,
    deferred_exit_edge_ids: frozenset[str] = frozenset(),
) -> TokenEngineSnapshot:
    """Advance one ready frame or finalize its loop."""
    return _close_ready_loop(
        snapshot,
        loop_instance_id,
        continuation_config=continuation_config,
        reducer=reducer,
        claimed_reduction=claimed_reduction,
        deferred_exit_edge_ids=deferred_exit_edge_ids,
    )


async def close_ready_loop_with_cas(
    store: TokenSnapshotStore,
    run_id: str,
    loop_instance_id: str,
    *,
    continuation_config: JoinConfig | None = None,
    reducer: LoopReducer = reduce_join_inputs,
    claim_owner_id: str | None = None,
    claimed_reduction: LoopReductionClaim | None = None,
    max_attempts: int = 8,
) -> TokenEngineSnapshot:
    """Claim and close a ready loop barrier through snapshot CAS."""
    return await _close_ready_loop_with_cas(
        store,
        run_id,
        loop_instance_id,
        continuation_config=continuation_config,
        reducer=reducer,
        claim_owner_id=claim_owner_id,
        claimed_reduction=claimed_reduction,
        max_attempts=max_attempts,
    )


async def reclaim_abandoned_loop_reduction_with_cas(
    store: TokenSnapshotStore,
    run_id: str,
    loop_instance_id: str,
    *,
    observed_claim: LoopReductionClaim,
    new_owner_id: str,
    max_attempts: int = 8,
) -> LoopReductionClaim:
    """Replace one explicitly observed abandoned reducer claim through CAS."""
    return await _reclaim_abandoned_loop_reduction_with_cas(
        store,
        run_id,
        loop_instance_id,
        observed_claim=observed_claim,
        new_owner_id=new_owner_id,
        max_attempts=max_attempts,
    )


__all__ = [
    "FailureMode",
    "LoopReducer",
    "LoopReductionClaim",
    "LoopReductionClaimChangedError",
    "LoopReductionRecoveryError",
    "LoopReductionReleaseError",
    "TokenLoopTransitionError",
    "close_ready_loop",
    "close_ready_loop_with_cas",
    "enter_loop",
    "reclaim_abandoned_loop_reduction_with_cas",
    "settle_loop_member",
]
