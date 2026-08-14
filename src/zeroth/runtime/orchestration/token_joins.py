"""Public facade for cohort-aware structured-token join transitions."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import JsonValue

from zeroth.contracts.graph.models import JoinConfig
from zeroth.contracts.graph.token_snapshot import TokenEngineSnapshot
from zeroth.contracts.graph.tokens import JoinObligationOutcome
from zeroth.runtime.orchestration.token_join_arrivals import (
    deliver_to_join as _deliver_to_join,
)
from zeroth.runtime.orchestration.token_join_arrivals import (
    settle_join_without_delivery as _settle_join_without_delivery,
)
from zeroth.runtime.orchestration.token_join_closure import (
    close_ready_join as _close_ready_join,
)
from zeroth.runtime.orchestration.token_join_closure import (
    close_ready_join_with_cas as _close_ready_join_with_cas,
)
from zeroth.runtime.orchestration.token_join_closure import (
    reclaim_abandoned_join_reduction_with_cas as _reclaim_abandoned_join_reduction_with_cas,
)
from zeroth.runtime.orchestration.token_join_models import (
    FailureMode,
    JoinReducer,
    JoinReducerInput,
    JoinReductionClaim,
    JoinReductionClaimChangedError,
    JoinReductionRecoveryError,
    JoinReductionReleaseError,
    TokenJoinTransitionError,
)
from zeroth.runtime.orchestration.token_join_reducers import (
    reduce_join_inputs as _reduce_join_inputs,
)
from zeroth.runtime.orchestration.token_lifecycle import CAS_MAX_ATTEMPTS
from zeroth.runtime.orchestration.token_snapshot_store import TokenSnapshotStore


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
    return _deliver_to_join(
        snapshot,
        target_node_id=target_node_id,
        inbound_edge_id=inbound_edge_id,
        cohort_inbound_edges=cohort_inbound_edges,
        payload=payload,
        token_id=token_id,
        dispatch_id=dispatch_id,
        attempt=attempt,
        cancellation_generation=cancellation_generation,
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
    return _settle_join_without_delivery(
        snapshot,
        target_node_id=target_node_id,
        inbound_edge_id=inbound_edge_id,
        cohort_inbound_edges=cohort_inbound_edges,
        outcome=outcome,
        token_id=token_id,
        dispatch_id=dispatch_id,
        attempt=attempt,
        cancellation_generation=cancellation_generation,
        failure_mode=failure_mode,
    )


def reduce_join_inputs(config: JoinConfig, inputs: tuple[JoinReducerInput, ...]) -> JsonValue:
    """Adapt labelled engine inputs to the configured reducer contract."""
    return _reduce_join_inputs(config, inputs)


def close_ready_join(
    snapshot: TokenEngineSnapshot,
    join_instance_id: str,
    config: JoinConfig,
    *,
    reducer: JoinReducer = reduce_join_inputs,
    failure_mode: FailureMode | None = None,
    claimed_reduction: JoinReductionClaim | None = None,
) -> TokenEngineSnapshot:
    """Consume one READY cohort and publish at most one continuation revision."""
    return _close_ready_join(
        snapshot,
        join_instance_id,
        config,
        reducer=reducer,
        failure_mode=failure_mode,
        claimed_reduction=claimed_reduction,
    )


async def close_ready_join_with_cas(
    store: TokenSnapshotStore,
    run_id: str,
    join_instance_id: str,
    config: JoinConfig,
    *,
    reducer: JoinReducer = reduce_join_inputs,
    failure_mode: FailureMode | None = None,
    claim_owner_id: str | None = None,
    claimed_reduction: JoinReductionClaim | None = None,
    max_attempts: int = CAS_MAX_ATTEMPTS,
) -> TokenEngineSnapshot:
    """Claim a reducer and close a READY join through snapshot CAS."""
    return await _close_ready_join_with_cas(
        store,
        run_id,
        join_instance_id,
        config,
        reducer=reducer,
        failure_mode=failure_mode,
        claim_owner_id=claim_owner_id,
        claimed_reduction=claimed_reduction,
        max_attempts=max_attempts,
    )


async def reclaim_abandoned_join_reduction_with_cas(
    store: TokenSnapshotStore,
    run_id: str,
    join_instance_id: str,
    *,
    observed_claim: JoinReductionClaim,
    new_owner_id: str,
    max_attempts: int = CAS_MAX_ATTEMPTS,
) -> JoinReductionClaim:
    """Explicitly replace one observed abandoned reducer claim through CAS."""
    return await _reclaim_abandoned_join_reduction_with_cas(
        store,
        run_id,
        join_instance_id,
        observed_claim=observed_claim,
        new_owner_id=new_owner_id,
        max_attempts=max_attempts,
    )


__all__ = [
    "FailureMode",
    "JoinReductionClaim",
    "JoinReductionClaimChangedError",
    "JoinReductionRecoveryError",
    "JoinReductionReleaseError",
    "JoinReducer",
    "JoinReducerInput",
    "TokenJoinTransitionError",
    "close_ready_join",
    "close_ready_join_with_cas",
    "deliver_to_join",
    "reduce_join_inputs",
    "reclaim_abandoned_join_reduction_with_cas",
    "settle_join_without_delivery",
]
