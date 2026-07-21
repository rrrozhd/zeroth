"""CAS ownership, recovery, and reducer execution for loop barriers."""

from __future__ import annotations

import uuid

from pydantic import JsonValue

from zeroth.contracts.graph.models import JoinConfig
from zeroth.contracts.graph.tokens import IterationFrameState, LoopInstance, LoopLifecycleState
from zeroth.runtime.orchestration.token_join_models import JoinReducerInput
from zeroth.runtime.orchestration.token_join_reducers import reduce_join_inputs
from zeroth.runtime.orchestration.token_loop_closure import (
    _claim_matches,
    _config_fingerprint,
    _inputs,
    _json_value,
    _loop,
    close_ready_loop,
)
from zeroth.runtime.orchestration.token_loop_helpers import (
    model_data,
    next_snapshot,
    replace_loop,
)
from zeroth.runtime.orchestration.token_loop_models import (
    LoopReducer,
    LoopReductionClaim,
    LoopReductionClaimChangedError,
    LoopReductionRecoveryError,
    LoopReductionReleaseError,
    TokenLoopTransitionError,
)
from zeroth.runtime.orchestration.token_snapshot_store import (
    TokenSnapshotConcurrencyError,
    TokenSnapshotStore,
)


async def _claim_loop_with_cas(
    store: TokenSnapshotStore,
    run_id: str,
    loop_instance_id: str,
    config: JoinConfig,
    *,
    owner_id: str,
    max_attempts: int,
) -> LoopReductionClaim:
    claim_id = uuid.uuid4().hex
    for _ in range(max_attempts):
        current = await store.get_token_snapshot(run_id)
        if current is None:
            raise LoopReductionRecoveryError(f"run {run_id!r} has no token snapshot")
        loop = _loop(current, loop_instance_id)
        if loop.reduction_claim_id is not None:
            if loop.reduction_claim_owner_id == owner_id:
                return LoopReductionClaim.from_loop(loop)
            raise LoopReductionRecoveryError("loop reduction is claimed by another owner")
        if loop.frames[-1].state is not IterationFrameState.BARRIER_READY:
            raise TokenLoopTransitionError("loop iteration barrier is not ready")
        revision = current.revision + 1
        claim = LoopReductionClaim(
            claim_id=claim_id,
            owner_id=owner_id,
            attempt=loop.reduction_attempt + 1,
            claimed_revision=revision,
        )
        replacement = LoopInstance.model_validate(
            {
                **model_data(loop),
                "reducer_fingerprint": _config_fingerprint(config),
                "reduction_claim_id": claim.claim_id,
                "reduction_claim_owner_id": claim.owner_id,
                "reduction_claim_revision": claim.claimed_revision,
                "reduction_attempt": claim.attempt,
                "updated_revision": revision,
            }
        )
        proposed = next_snapshot(current, loops=replace_loop(current.loops, replacement))
        try:
            await store.compare_and_swap_token_snapshot(
                run_id, expected_revision=current.revision, snapshot=proposed
            )
        except TokenSnapshotConcurrencyError:
            continue
        return claim
    raise LoopReductionRecoveryError("loop reduction claim exhausted CAS attempts")


async def _release_claim_with_cas(
    store: TokenSnapshotStore,
    run_id: str,
    loop_instance_id: str,
    claim: LoopReductionClaim,
    *,
    max_attempts: int,
):
    for _ in range(max_attempts):
        current = await store.get_token_snapshot(run_id)
        if current is None:
            raise LoopReductionReleaseError(f"run {run_id!r} has no token snapshot")
        loop = _loop(current, loop_instance_id)
        if not _claim_matches(loop, claim):
            raise LoopReductionClaimChangedError("loop reduction claim changed before release")
        revision = current.revision + 1
        replacement = LoopInstance.model_validate(
            {
                **model_data(loop),
                "reducer_fingerprint": None,
                "reduction_claim_id": None,
                "reduction_claim_owner_id": None,
                "reduction_claim_revision": None,
                "updated_revision": revision,
            }
        )
        proposed = next_snapshot(current, loops=replace_loop(current.loops, replacement))
        try:
            return await store.compare_and_swap_token_snapshot(
                run_id, expected_revision=current.revision, snapshot=proposed
            )
        except TokenSnapshotConcurrencyError:
            continue
    raise LoopReductionReleaseError("loop reduction claim release exhausted CAS attempts")


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
):
    """Claim and evaluate a continuation reducer once, then publish through CAS."""
    current = await store.get_token_snapshot(run_id)
    if current is None:
        raise TokenLoopTransitionError(f"run {run_id!r} has no token snapshot")
    loop = _loop(current, loop_instance_id)
    if loop.lifecycle_state is LoopLifecycleState.COMPLETED:
        return current
    inputs = _inputs(current, loop)
    if not inputs:
        for _ in range(max_attempts):
            current = await store.get_token_snapshot(run_id)
            if current is None:
                raise TokenLoopTransitionError(f"run {run_id!r} has no token snapshot")
            proposed = close_ready_loop(current, loop_instance_id)
            if proposed is current:
                return current
            try:
                return await store.compare_and_swap_token_snapshot(
                    run_id, expected_revision=current.revision, snapshot=proposed
                )
            except TokenSnapshotConcurrencyError:
                continue
        raise TokenSnapshotConcurrencyError(
            "loop finalization exhausted CAS attempts",
            expected_revision=current.revision,
            actual_revision=current.revision,
        )
    config = continuation_config
    if len(inputs) > 1 and config is None:
        raise TokenLoopTransitionError(
            "multiple back-edge deliveries require destination header JoinConfig"
        )
    config = config or JoinConfig()
    active_claim = claimed_reduction or await _claim_loop_with_cas(
        store,
        run_id,
        loop_instance_id,
        config,
        owner_id=claim_owner_id or uuid.uuid4().hex,
        max_attempts=max_attempts,
    )
    claimed = await store.get_token_snapshot(run_id)
    if claimed is None:
        raise TokenLoopTransitionError(f"run {run_id!r} has no token snapshot")
    claimed_loop = _loop(claimed, loop_instance_id)
    if not _claim_matches(claimed_loop, active_claim):
        raise LoopReductionClaimChangedError("loop reduction claim changed before evaluation")
    try:
        reduced = _json_value(reducer(config, _inputs(claimed, claimed_loop)))
    except Exception:
        try:
            await _release_claim_with_cas(
                store,
                run_id,
                loop_instance_id,
                active_claim,
                max_attempts=max_attempts,
            )
        except Exception as exc:
            raise LoopReductionReleaseError(
                "failed loop reducer claim could not be atomically released"
            ) from exc
        raise

    def prepared(_config: JoinConfig, _items: tuple[JoinReducerInput, ...]) -> JsonValue:
        return reduced

    for _ in range(max_attempts):
        current = await store.get_token_snapshot(run_id)
        if current is None:
            raise TokenLoopTransitionError(f"run {run_id!r} has no token snapshot")
        proposed = close_ready_loop(
            current,
            loop_instance_id,
            continuation_config=config,
            reducer=prepared,
            claimed_reduction=active_claim,
        )
        try:
            return await store.compare_and_swap_token_snapshot(
                run_id, expected_revision=current.revision, snapshot=proposed
            )
        except TokenSnapshotConcurrencyError:
            continue
    raise TokenSnapshotConcurrencyError(
        "loop closure exhausted CAS attempts",
        expected_revision=current.revision,
        actual_revision=current.revision,
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
    """Replace exactly one explicitly observed abandoned claim through CAS."""
    if not new_owner_id:
        raise LoopReductionRecoveryError("recovery owner cannot be empty")
    replacement_id = uuid.uuid4().hex
    for _ in range(max_attempts):
        current = await store.get_token_snapshot(run_id)
        if current is None:
            raise LoopReductionRecoveryError(f"run {run_id!r} has no token snapshot")
        loop = _loop(current, loop_instance_id)
        if not _claim_matches(loop, observed_claim):
            raise LoopReductionClaimChangedError("loop reduction claim changed before recovery")
        revision = current.revision + 1
        replacement_claim = LoopReductionClaim(
            claim_id=replacement_id,
            owner_id=new_owner_id,
            attempt=observed_claim.attempt + 1,
            claimed_revision=revision,
        )
        replacement = LoopInstance.model_validate(
            {
                **model_data(loop),
                "reduction_claim_id": replacement_claim.claim_id,
                "reduction_claim_owner_id": replacement_claim.owner_id,
                "reduction_claim_revision": replacement_claim.claimed_revision,
                "reduction_attempt": replacement_claim.attempt,
                "updated_revision": revision,
            }
        )
        proposed = next_snapshot(current, loops=replace_loop(current.loops, replacement))
        try:
            await store.compare_and_swap_token_snapshot(
                run_id, expected_revision=current.revision, snapshot=proposed
            )
        except TokenSnapshotConcurrencyError:
            continue
        return replacement_claim
    raise LoopReductionClaimChangedError("loop reduction recovery exhausted CAS attempts")


__all__ = [
    "close_ready_loop_with_cas",
    "reclaim_abandoned_loop_reduction_with_cas",
]
