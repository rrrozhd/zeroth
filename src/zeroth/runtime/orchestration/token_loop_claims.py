"""CAS ownership, recovery, and reducer execution for loop barriers."""

from __future__ import annotations

import asyncio
import uuid

from pydantic import JsonValue

from zeroth.contracts.graph.models import JoinConfig
from zeroth.contracts.graph.tokens import (
    IterationFrameState,
    LoopInstance,
    LoopLifecycleState,
)
from zeroth.runtime.orchestration.token_join_models import JoinReducerInput
from zeroth.runtime.orchestration.token_join_reducers import reduce_join_inputs
from zeroth.runtime.orchestration.token_lifecycle import (
    CAS_MAX_ATTEMPTS,
    CasSleep,
    cas_backoff,
)
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


async def _pace_retry(attempt: int, max_attempts: int, sleep: CasSleep) -> None:
    """Wait before the next CAS attempt, and never after the last one.

    Every loop in this module retries a lost CAS against the same
    :class:`TokenSnapshotStore` the lifecycle adapter uses, so they must pace
    themselves the same way: retrying immediately makes contention a livelock,
    and a capped loop that retries immediately just fails fast without ever
    giving the retry a chance to win. The backoff and jitter math is not
    duplicated here -- it stays in :func:`cas_backoff`; this only decides
    whether the caller still has an attempt left worth pacing.

    Args:
        attempt: 1-based number of the attempt that just lost.
        max_attempts: The loop's total retry budget.
        sleep: Injected so tests can observe the delay instead of spending it.
    """
    if attempt < max_attempts:
        await cas_backoff(attempt, sleep=sleep)


async def _claim_loop_with_cas(
    store: TokenSnapshotStore,
    run_id: str,
    loop_instance_id: str,
    config: JoinConfig,
    *,
    owner_id: str,
    max_attempts: int,
    sleep: CasSleep = asyncio.sleep,
) -> LoopReductionClaim:
    claim_id = uuid.uuid4().hex
    for attempt in range(1, max_attempts + 1):
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
            await _pace_retry(attempt, max_attempts, sleep)
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
    sleep: CasSleep = asyncio.sleep,
):
    for attempt in range(1, max_attempts + 1):
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
            await _pace_retry(attempt, max_attempts, sleep)
            continue
    raise LoopReductionReleaseError("loop reduction claim release exhausted CAS attempts")


def _require_positive_attempts(max_attempts: int) -> None:
    """Reject a non-positive retry budget.

    Kept out of ``close_ready_loop_with_cas`` deliberately: that function is already
    over the complexity ceiling the commit gate ratchets against, and a guard clause
    inline would raise it further for no gain in clarity.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")


async def close_ready_loop_with_cas(
    store: TokenSnapshotStore,
    run_id: str,
    loop_instance_id: str,
    *,
    continuation_config: JoinConfig | None = None,
    reducer: LoopReducer = reduce_join_inputs,
    claim_owner_id: str | None = None,
    claimed_reduction: LoopReductionClaim | None = None,
    max_attempts: int = CAS_MAX_ATTEMPTS,
    sleep: CasSleep = asyncio.sleep,
):
    """Claim and evaluate a continuation reducer once, then publish through CAS."""
    _require_positive_attempts(max_attempts)
    current = await store.get_token_snapshot(run_id)
    if current is None:
        raise TokenLoopTransitionError(f"run {run_id!r} has no token snapshot")
    loop = _loop(current, loop_instance_id)
    if loop.lifecycle_state is LoopLifecycleState.COMPLETED:
        return current
    inputs = _inputs(current, loop)
    if not inputs or (len(inputs) == 1 and continuation_config is None):
        # Re-raise the CAS error the store actually reported: it carries the run
        # id and the two genuinely different revisions. Rebuilding it here put a
        # message string where the run id belongs and reported the same revision
        # as both expected and actual, so the exhaustion read "expected N, found N".
        finalization_error: TokenSnapshotConcurrencyError | None = None
        for attempt in range(1, max_attempts + 1):
            current = await store.get_token_snapshot(run_id)
            if current is None:
                raise TokenLoopTransitionError(f"run {run_id!r} has no token snapshot")
            proposed = close_ready_loop(
                current,
                loop_instance_id,
                continuation_config=continuation_config,
            )
            if proposed is current:
                return current
            try:
                return await store.compare_and_swap_token_snapshot(
                    run_id, expected_revision=current.revision, snapshot=proposed
                )
            except TokenSnapshotConcurrencyError as exc:
                finalization_error = exc
                await _pace_retry(attempt, max_attempts, sleep)
                continue
        assert finalization_error is not None
        raise finalization_error
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
        sleep=sleep,
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
                sleep=sleep,
            )
        except Exception as exc:
            raise LoopReductionReleaseError(
                "failed loop reducer claim could not be atomically released"
            ) from exc
        raise

    def prepared(_config: JoinConfig, _items: tuple[JoinReducerInput, ...]) -> JsonValue:
        return reduced

    closure_error: TokenSnapshotConcurrencyError | None = None
    for attempt in range(1, max_attempts + 1):
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
        except TokenSnapshotConcurrencyError as exc:
            # Same defect as the finalization branch above: the store's own error
            # is the only one that knows the run id and the two revisions that
            # actually differed.
            closure_error = exc
            await _pace_retry(attempt, max_attempts, sleep)
            continue
    assert closure_error is not None
    raise closure_error


async def reclaim_abandoned_loop_reduction_with_cas(
    store: TokenSnapshotStore,
    run_id: str,
    loop_instance_id: str,
    *,
    observed_claim: LoopReductionClaim,
    new_owner_id: str,
    max_attempts: int = CAS_MAX_ATTEMPTS,
    sleep: CasSleep = asyncio.sleep,
) -> LoopReductionClaim:
    """Replace exactly one explicitly observed abandoned claim through CAS."""
    if not new_owner_id:
        raise LoopReductionRecoveryError("recovery owner cannot be empty")
    replacement_id = uuid.uuid4().hex
    for attempt in range(1, max_attempts + 1):
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
            await _pace_retry(attempt, max_attempts, sleep)
            continue
        return replacement_claim
    raise LoopReductionClaimChangedError("loop reduction recovery exhausted CAS attempts")


__all__ = [
    "close_ready_loop_with_cas",
    "reclaim_abandoned_loop_reduction_with_cas",
]
