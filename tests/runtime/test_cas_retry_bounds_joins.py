"""Join-side CAS retry loops pace themselves between attempts (F-08, R24-CAS).

``token_join_closure`` is the join-side mirror of the loop-side defect covered by
``test_cas_retry_bounds``: every retry here already had an attempt cap, so a
termination test passes against the unpaced code.  What they lacked was any
delay *between* attempts -- a lost CAS was retried immediately, which burns the
whole budget in microseconds and raises without ever giving a retry a chance to
win, and a contended reduction claim was polled with ``asyncio.sleep(0)``.

Each test asserts the exact sleep count (``max_attempts - 1``: between attempts,
never after the last one) so a revert fails cleanly instead of hanging, and
re-asserts the exhaustion error type so pacing cannot quietly change what
callers see.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest

from zeroth.contracts.graph.models import JoinConfig
from zeroth.contracts.graph.token_snapshot import TokenEngineSnapshot
from zeroth.contracts.graph.tokens import JoinLifecycleState
from zeroth.runtime.orchestration.token_join_arrivals import deliver_to_join
from zeroth.runtime.orchestration.token_join_closure import (
    close_ready_join_with_cas,
    reclaim_abandoned_join_reduction_with_cas,
)
from zeroth.runtime.orchestration.token_join_models import (
    JoinReductionClaim,
    JoinReductionClaimChangedError,
    JoinReductionReleaseError,
    TokenJoinTransitionError,
)
from zeroth.runtime.orchestration.token_lifecycle import (
    CAS_BASE_DELAY_SECONDS,
    CAS_MAX_ATTEMPTS,
    CAS_MAX_DELAY_SECONDS,
)
from zeroth.runtime.orchestration.token_scheduler import (
    FanOutBranch,
    claim_next_token,
    fan_out_dispatch,
    initialize_token_snapshot,
)
from zeroth.runtime.orchestration.token_snapshot_store import (
    TokenSnapshotConcurrencyError,
)

# Well above any sane attempt cap, low enough that an unbounded loop is caught
# in milliseconds instead of hanging the suite.
SPIN_GUARD = 64

TEST_TIMEOUT = 5.0


class _UnboundedRetryError(RuntimeError):
    """Raised by the fake store when a caller retries CAS without a cap."""


class _ContendedStore:
    """A snapshot store whose CAS always loses to a concurrent writer.

    ``get_token_snapshot`` always returns a live snapshot: the loops under test
    raise when a reload comes back ``None``, so a ``None``-returning store would
    terminate them for the wrong reason and prove nothing.
    """

    def __init__(self, snapshot: TokenEngineSnapshot, *, allow_commits: int = 0) -> None:
        self.snapshot = snapshot
        self.cas_calls = 0
        self.allow_commits = allow_commits

    async def get_token_snapshot(self, run_id: str):
        return self.snapshot if run_id == self.snapshot.run_id else None

    async def compare_and_swap_token_snapshot(self, run_id: str, *, expected_revision, snapshot):
        self.cas_calls += 1
        if self.cas_calls > SPIN_GUARD:
            raise _UnboundedRetryError(
                f"CAS retried {self.cas_calls} times: the loop has no attempt cap"
            )
        if self.allow_commits > 0 and expected_revision == self.snapshot.revision:
            self.allow_commits -= 1
            self.snapshot = snapshot
            return snapshot
        # A genuine race: the persisted revision has moved past what we expected.
        raise TokenSnapshotConcurrencyError(
            run_id,
            expected_revision=expected_revision,
            actual_revision=self.snapshot.revision + 1,
        )


class _RecordingSleep:
    """An injectable ``asyncio.sleep`` stand-in that records requested delays."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def _ceilings(attempts: int) -> list[float]:
    return [
        min(CAS_BASE_DELAY_SECONDS * (2 ** (attempt - 1)), CAS_MAX_DELAY_SECONDS)
        for attempt in range(1, attempts + 1)
    ]


def _assert_paced(sleeper: _RecordingSleep, attempts: int) -> None:
    """Assert one sleep between every pair of attempts, bounded and jittered."""
    assert len(sleeper.delays) == attempts - 1
    ceilings = _ceilings(attempts - 1)
    pairs = list(zip(sleeper.delays, ceilings, strict=True))
    assert all(0.0 <= delay <= ceiling for delay, ceiling in pairs)
    # Full jitter draws uniformly below the ceiling, so a fixed-delay or
    # ceiling-only implementation cannot produce a strictly smaller sample.
    assert any(delay < ceiling for delay, ceiling in pairs), "backoff is not jittered"
    assert len(set(sleeper.delays)) > 1


def _fanout(run_id: str, width: int = 2) -> TokenEngineSnapshot:
    initial = initialize_token_snapshot(run_id=run_id, root_node_id="entry", payload={"root": True})
    claim = claim_next_token(initial)
    return fan_out_dispatch(
        claim.snapshot,
        dispatch_id=claim.dispatch.dispatch_id,
        attempt=0,
        cancellation_generation=0,
        branches=tuple(
            FanOutBranch(node_id=f"branch-{i}", inbound_edge_id=f"split-{i}", payload={"i": i})
            for i in range(width)
        ),
    )


def _routes(snapshot: TokenEngineSnapshot, target: str = "join") -> Mapping[str, str]:
    fork = snapshot.forks[-1]
    return {child.token_id: f"{target}-edge-{child.creation_ordinal}" for child in fork.children}


def _deliver_head(
    snapshot: TokenEngineSnapshot, *, payload: object, target: str = "join"
) -> TokenEngineSnapshot:
    routes = _routes(snapshot, target)
    claim = claim_next_token(snapshot)
    token_id = claim.dispatch.token.token_id
    return deliver_to_join(
        claim.snapshot,
        dispatch_id=claim.dispatch.dispatch_id,
        attempt=claim.dispatch.attempt,
        cancellation_generation=claim.dispatch.cancellation_generation,
        target_node_id=target,
        inbound_edge_id=routes[token_id],
        cohort_inbound_edges=routes,
        payload=payload,
    )


def _ready_join(run_id: str) -> TokenEngineSnapshot:
    """A two-arrival cohort whose join is READY for reduction."""
    return _deliver_head(_deliver_head(_fanout(run_id), payload={"a": 1}), payload={"b": 2})


def _crashing_reducer(_config, _inputs):
    raise RuntimeError("reducer failed")


async def _reducing_store(run_id: str, owner_id: str = "dead-worker") -> _ContendedStore:
    """Leave the join durably REDUCING under a claim nobody will release.

    One winning CAS lands the reduction claim; the single follow-up attempt then
    loses, which is what a closer that crashed after claiming leaves behind.
    """
    ready = _ready_join(run_id)
    store = _ContendedStore(ready, allow_commits=1)
    with pytest.raises(TokenSnapshotConcurrencyError):
        await close_ready_join_with_cas(
            store,
            ready.run_id,
            ready.joins[0].join_instance_id,
            JoinConfig(),
            claim_owner_id=owner_id,
            max_attempts=1,
        )
    assert store.snapshot.joins[0].lifecycle_state is JoinLifecycleState.REDUCING
    store.cas_calls = 0
    return store


async def test_join_reduction_claim_sleeps_between_attempts_but_not_after_the_last() -> None:
    ready = _ready_join("run-join-claim-sleep")
    store = _ContendedStore(ready)
    sleeper = _RecordingSleep()

    with pytest.raises(TokenSnapshotConcurrencyError) as caught:
        await asyncio.wait_for(
            close_ready_join_with_cas(
                store,
                ready.run_id,
                ready.joins[0].join_instance_id,
                JoinConfig(),
                sleep=sleeper,
            ),
            TEST_TIMEOUT,
        )

    assert caught.value.run_id == ready.run_id
    assert store.cas_calls == CAS_MAX_ATTEMPTS
    _assert_paced(sleeper, CAS_MAX_ATTEMPTS)


async def test_join_reduction_poll_sleeps_between_attempts_but_not_after_the_last() -> None:
    # The claim is held by another live closer, so this closer never reaches CAS
    # at all: it reloads and re-observes the same REDUCING join every attempt.
    # That poll used to be ``await asyncio.sleep(0)`` -- a cap with no delay.
    store = await _reducing_store("run-join-poll-sleep")
    sleeper = _RecordingSleep()

    with pytest.raises(TokenJoinTransitionError, match="claimed by another live closer"):
        await asyncio.wait_for(
            close_ready_join_with_cas(
                store,
                store.snapshot.run_id,
                store.snapshot.joins[0].join_instance_id,
                JoinConfig(),
                sleep=sleeper,
            ),
            TEST_TIMEOUT,
        )

    assert store.cas_calls == 0
    _assert_paced(sleeper, CAS_MAX_ATTEMPTS)


async def test_join_claim_release_sleeps_between_attempts_but_not_after_the_last() -> None:
    ready = _ready_join("run-join-release-sleep")
    # The claim wins on its first attempt, the reducer then fails, and every
    # recorded delay belongs to the release loop that cannot give the claim back.
    store = _ContendedStore(ready, allow_commits=1)
    sleeper = _RecordingSleep()

    with pytest.raises(JoinReductionReleaseError, match="atomically returned to READY"):
        await asyncio.wait_for(
            close_ready_join_with_cas(
                store,
                ready.run_id,
                ready.joins[0].join_instance_id,
                JoinConfig(),
                reducer=_crashing_reducer,
                claim_owner_id="worker-a",
                sleep=sleeper,
            ),
            TEST_TIMEOUT,
        )

    assert store.cas_calls == CAS_MAX_ATTEMPTS + 1
    _assert_paced(sleeper, CAS_MAX_ATTEMPTS)


async def test_join_closure_transition_sleeps_between_attempts_but_not_after_the_last() -> None:
    # The claim wins, the reducer succeeds, and the publish goes through
    # ``apply_token_transition``: the injected sleep has to reach it too, or the
    # final leg of the close is the one unpaced retry left in the path.
    ready = _ready_join("run-join-closure-sleep")
    store = _ContendedStore(ready, allow_commits=1)
    sleeper = _RecordingSleep()

    with pytest.raises(TokenSnapshotConcurrencyError) as caught:
        await asyncio.wait_for(
            close_ready_join_with_cas(
                store,
                ready.run_id,
                ready.joins[0].join_instance_id,
                JoinConfig(),
                claim_owner_id="worker-a",
                sleep=sleeper,
            ),
            TEST_TIMEOUT,
        )

    assert caught.value.run_id == ready.run_id
    assert store.cas_calls == CAS_MAX_ATTEMPTS + 1
    _assert_paced(sleeper, CAS_MAX_ATTEMPTS)


async def test_join_reduction_reclaim_sleeps_between_attempts_but_not_after_the_last() -> None:
    store = await _reducing_store("run-join-reclaim-sleep")
    observed = JoinReductionClaim.from_join(store.snapshot.joins[0])
    sleeper = _RecordingSleep()

    with pytest.raises(JoinReductionClaimChangedError, match="exhausted CAS attempts"):
        await asyncio.wait_for(
            reclaim_abandoned_join_reduction_with_cas(
                store,
                store.snapshot.run_id,
                store.snapshot.joins[0].join_instance_id,
                observed_claim=observed,
                new_owner_id="recovery-worker",
                sleep=sleeper,
            ),
            TEST_TIMEOUT,
        )

    assert store.cas_calls == CAS_MAX_ATTEMPTS
    _assert_paced(sleeper, CAS_MAX_ATTEMPTS)
