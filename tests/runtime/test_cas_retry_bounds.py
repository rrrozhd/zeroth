"""CAS retry loops terminate under sustained contention (R24-CAS, A16-11, A06-14, A16-23).

Every loop covered here used to be a bare ``while True:`` that retried a lost
snapshot CAS forever.  Sustained contention therefore wedged a coordinator --
and for :class:`TokenLifecycleAdapter` an *operator* pause/resume/stop/cancel --
with no attempt cap, no backoff and no jitter.

The termination tests deliberately depend only on the pre-existing public API so
they fail against unmodified code by spinning (the store's spin guard trips)
rather than by failing to import a new symbol.
"""

from __future__ import annotations

import asyncio

import pytest

from zeroth.contracts.graph.models import JoinConfig
from zeroth.contracts.graph.tokens import IterationMemberState
from zeroth.runtime.orchestration.token_lifecycle import (
    TokenLifecycleAdapter,
    pause_snapshot,
)
from zeroth.runtime.orchestration.token_loop_claims import (
    close_ready_loop_with_cas,
    reclaim_abandoned_loop_reduction_with_cas,
)
from zeroth.runtime.orchestration.token_loop_models import (
    LoopReductionClaim,
    LoopReductionClaimChangedError,
    LoopReductionRecoveryError,
    LoopReductionReleaseError,
)
from zeroth.runtime.orchestration.token_loops import enter_loop, settle_loop_member
from zeroth.runtime.orchestration.token_runtime import TokenRuntimeCoordinator
from zeroth.runtime.orchestration.token_scheduler import (
    FanOutBranch,
    apply_token_transition,
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

# A bounded loop must stay far below the guard. Deliberately not the production
# constant: these tests must be runnable against unmodified code.
BOUNDED_ATTEMPTS = 16

TEST_TIMEOUT = 5.0


class _UnboundedRetryError(RuntimeError):
    """Raised by the fake store when a caller retries CAS without a cap."""


class _ContendedStore:
    """A snapshot store whose CAS always loses to a concurrent writer.

    ``get_token_snapshot`` always returns a live snapshot: the in-scope loops
    raise :class:`OrchestratorError` when a reload comes back ``None``, so a
    ``None``-returning store would terminate them for the wrong reason and
    prove nothing.
    """

    def __init__(self, snapshot, *, allow_commits: int = 0) -> None:
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


def _root(run_id: str = "run-cas"):
    return initialize_token_snapshot(run_id=run_id, root_node_id="root", payload={"n": 0})


def _claimed(run_id: str = "run-cas"):
    return claim_next_token(_root(run_id)).snapshot


def _loop_with_single_back_edge(run_id: str = "run-loop"):
    root = initialize_token_snapshot(run_id=run_id, root_node_id="header", payload={"n": 0})
    entered = enter_loop(
        root,
        token_id=root.tokens[0].token_id,
        loop_header_node_id="header",
        body_node_id="body",
        inbound_edge_id="header-body",
        exit_routes={"exit": "after"},
    )
    return settle_loop_member(
        entered,
        token_id=entered.queue[0].token_id,
        outcome=IterationMemberState.BACK_EDGE_CONTINUATION,
        edge_id="back",
        payload={"single": True},
    )


def _loop_with_two_back_edges(run_id: str = "run-loop-many"):
    root = initialize_token_snapshot(run_id=run_id, root_node_id="header", payload={"n": 0})
    entered = enter_loop(
        root,
        token_id=root.tokens[0].token_id,
        loop_header_node_id="header",
        body_node_id="body",
        inbound_edge_id="header-body",
        exit_routes={"exit": "after"},
    )
    claim = claim_next_token(entered)
    snapshot = fan_out_dispatch(
        claim.snapshot,
        dispatch_id=claim.dispatch.dispatch_id,
        attempt=0,
        cancellation_generation=0,
        branches=tuple(
            FanOutBranch(
                node_id=f"branch-{index}",
                inbound_edge_id=f"body-{index}",
                payload={"branch": index},
            )
            for index in range(2)
        ),
    )
    for child in snapshot.forks[0].children:
        snapshot = settle_loop_member(
            snapshot,
            token_id=child.token_id,
            outcome=IterationMemberState.BACK_EDGE_CONTINUATION,
            edge_id="back",
            payload=child.creation_ordinal,
        )
    return snapshot


# --------------------------------------------------------------------------
# R24-CAS / A06-14 -- token_runtime.py claim, recover and transition loops
# --------------------------------------------------------------------------


async def test_claim_retry_terminates_under_sustained_conflict() -> None:
    snapshot = _root()
    store = _ContendedStore(snapshot)
    coordinator = TokenRuntimeCoordinator(object(), store)

    with pytest.raises(TokenSnapshotConcurrencyError) as caught:
        await asyncio.wait_for(coordinator._claim(snapshot), TEST_TIMEOUT)

    assert store.cas_calls <= BOUNDED_ATTEMPTS
    assert caught.value.run_id == snapshot.run_id


async def test_recover_retry_terminates_under_sustained_conflict() -> None:
    snapshot = _claimed()
    store = _ContendedStore(snapshot)
    coordinator = TokenRuntimeCoordinator(object(), store)

    with pytest.raises(TokenSnapshotConcurrencyError) as caught:
        await asyncio.wait_for(coordinator._recover(snapshot), TEST_TIMEOUT)

    assert store.cas_calls <= BOUNDED_ATTEMPTS
    assert caught.value.run_id == snapshot.run_id


async def test_transition_retry_terminates_under_sustained_conflict() -> None:
    snapshot = _root()
    store = _ContendedStore(snapshot)
    coordinator = TokenRuntimeCoordinator(object(), store)

    with pytest.raises(TokenSnapshotConcurrencyError):
        await asyncio.wait_for(coordinator._transition(snapshot, pause_snapshot), TEST_TIMEOUT)

    assert store.cas_calls <= BOUNDED_ATTEMPTS


# --------------------------------------------------------------------------
# A16-11 -- operator lifecycle commands must not spin forever
# --------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["pause", "resume", "stop", "cancel"])
async def test_operator_lifecycle_command_terminates_under_sustained_conflict(
    command: str,
) -> None:
    snapshot = _claimed(f"run-operator-{command}")
    if command == "resume":
        # ``resume_snapshot`` is a no-op on a running snapshot and would never
        # reach CAS; the operator only resumes something already paused.
        snapshot = pause_snapshot(snapshot)
    store = _ContendedStore(snapshot)
    adapter = TokenLifecycleAdapter(store)

    with pytest.raises(TokenSnapshotConcurrencyError) as caught:
        await asyncio.wait_for(getattr(adapter, command)(snapshot.run_id), TEST_TIMEOUT)

    assert store.cas_calls <= BOUNDED_ATTEMPTS
    assert caught.value.run_id == snapshot.run_id


# --------------------------------------------------------------------------
# A16-23 -- exhaustion errors must carry the run id and differing revisions
# --------------------------------------------------------------------------


async def test_loop_finalization_exhaustion_reports_run_id_and_differing_revisions() -> None:
    snapshot = _loop_with_single_back_edge()
    store = _ContendedStore(snapshot)

    with pytest.raises(TokenSnapshotConcurrencyError) as caught:
        await asyncio.wait_for(
            close_ready_loop_with_cas(store, snapshot.run_id, snapshot.loops[0].loop_instance_id),
            TEST_TIMEOUT,
        )

    assert caught.value.run_id == snapshot.run_id
    assert caught.value.expected_revision != caught.value.actual_revision


async def test_loop_closure_exhaustion_reports_run_id_and_differing_revisions() -> None:
    snapshot = _loop_with_two_back_edges()
    # One commit lets the reduction claim land; every later CAS loses, which is
    # the only way to reach the closure loop's exhaustion branch.
    store = _ContendedStore(snapshot, allow_commits=1)

    with pytest.raises(TokenSnapshotConcurrencyError) as caught:
        await asyncio.wait_for(
            close_ready_loop_with_cas(
                store,
                snapshot.run_id,
                snapshot.loops[0].loop_instance_id,
                continuation_config=JoinConfig(),
                claim_owner_id="worker-a",
            ),
            TEST_TIMEOUT,
        )

    assert caught.value.run_id == snapshot.run_id
    assert caught.value.expected_revision != caught.value.actual_revision


# --------------------------------------------------------------------------
# Backoff and jitter (new behaviour -- imports the new surface locally so the
# termination tests above stay runnable against unmodified code)
# --------------------------------------------------------------------------


def _ceilings(attempts: int) -> list[float]:
    from zeroth.runtime.orchestration.token_lifecycle import (
        CAS_BASE_DELAY_SECONDS,
        CAS_MAX_DELAY_SECONDS,
    )

    return [
        min(CAS_BASE_DELAY_SECONDS * (2 ** (attempt - 1)), CAS_MAX_DELAY_SECONDS)
        for attempt in range(1, attempts + 1)
    ]


async def test_cas_backoff_is_bounded_exponential() -> None:
    from zeroth.runtime.orchestration.token_lifecycle import (
        CAS_MAX_ATTEMPTS,
        cas_backoff,
    )

    sleeper = _RecordingSleep()
    for attempt in range(1, CAS_MAX_ATTEMPTS + 1):
        await cas_backoff(attempt, sleep=sleeper)

    ceilings = _ceilings(CAS_MAX_ATTEMPTS)
    assert len(sleeper.delays) == CAS_MAX_ATTEMPTS
    assert all(
        0.0 <= delay <= ceiling for delay, ceiling in zip(sleeper.delays, ceilings, strict=True)
    )
    assert ceilings[1] > ceilings[0]
    assert max(ceilings) <= max(_ceilings(CAS_MAX_ATTEMPTS + 4))


async def test_cas_backoff_is_jittered_not_a_fixed_delay() -> None:
    from zeroth.runtime.orchestration.token_lifecycle import cas_backoff

    sleeper = _RecordingSleep()
    for _ in range(40):
        await cas_backoff(4, sleep=sleeper)

    ceiling = _ceilings(4)[-1]
    assert len(set(sleeper.delays)) > 1, "backoff is not jittered"
    assert all(0.0 <= delay <= ceiling for delay in sleeper.delays)


async def test_claim_sleeps_between_attempts_but_not_after_the_last() -> None:
    from zeroth.runtime.orchestration.token_lifecycle import CAS_MAX_ATTEMPTS

    snapshot = _root()
    store = _ContendedStore(snapshot)
    sleeper = _RecordingSleep()
    coordinator = TokenRuntimeCoordinator(object(), store, sleep=sleeper)

    with pytest.raises(TokenSnapshotConcurrencyError):
        await asyncio.wait_for(coordinator._claim(snapshot), TEST_TIMEOUT)

    assert store.cas_calls == CAS_MAX_ATTEMPTS
    assert len(sleeper.delays) == CAS_MAX_ATTEMPTS - 1
    assert all(
        0.0 <= delay <= ceiling
        for delay, ceiling in zip(sleeper.delays, _ceilings(CAS_MAX_ATTEMPTS - 1), strict=True)
    )


async def test_operator_pause_sleeps_between_attempts_but_not_after_the_last() -> None:
    from zeroth.runtime.orchestration.token_lifecycle import CAS_MAX_ATTEMPTS

    snapshot = _claimed("run-operator-sleep")
    store = _ContendedStore(snapshot)
    sleeper = _RecordingSleep()
    adapter = TokenLifecycleAdapter(store, sleep=sleeper)

    with pytest.raises(TokenSnapshotConcurrencyError):
        await asyncio.wait_for(adapter.pause(snapshot.run_id), TEST_TIMEOUT)

    assert store.cas_calls == CAS_MAX_ATTEMPTS
    assert len(sleeper.delays) == CAS_MAX_ATTEMPTS - 1


# --------------------------------------------------------------------------
# F-08 / R24-CAS -- capped-but-immediate loops are still unpaced retries.
#
# These six loops already had an attempt cap, so the termination tests above
# pass against them.  They retried with *zero* delay, which under contention
# burns the whole budget in microseconds and raises without ever giving the
# retry a chance to win.  Each test below asserts the exact sleep count --
# ``max_attempts - 1``, i.e. between attempts and never after the last one --
# so a revert fails cleanly instead of hanging, and re-asserts the exhaustion
# error type so pacing cannot quietly change what callers see.
# --------------------------------------------------------------------------


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


def _crashing_reducer(_config, _inputs):
    raise RuntimeError("reducer failed")


async def test_loop_finalization_sleeps_between_attempts_but_not_after_the_last() -> None:
    from zeroth.runtime.orchestration.token_lifecycle import CAS_MAX_ATTEMPTS

    snapshot = _loop_with_single_back_edge("run-loop-final-sleep")
    store = _ContendedStore(snapshot)
    sleeper = _RecordingSleep()

    with pytest.raises(TokenSnapshotConcurrencyError) as caught:
        await asyncio.wait_for(
            close_ready_loop_with_cas(
                store,
                snapshot.run_id,
                snapshot.loops[0].loop_instance_id,
                sleep=sleeper,
            ),
            TEST_TIMEOUT,
        )

    assert caught.value.run_id == snapshot.run_id
    assert store.cas_calls == CAS_MAX_ATTEMPTS
    _assert_paced(sleeper, CAS_MAX_ATTEMPTS)


async def test_loop_closure_sleeps_between_attempts_but_not_after_the_last() -> None:
    from zeroth.runtime.orchestration.token_lifecycle import CAS_MAX_ATTEMPTS

    snapshot = _loop_with_two_back_edges("run-loop-closure-sleep")
    # The single allowed commit lands the reduction claim on its first attempt,
    # so every recorded delay belongs to the closure loop that follows it.
    store = _ContendedStore(snapshot, allow_commits=1)
    sleeper = _RecordingSleep()

    with pytest.raises(TokenSnapshotConcurrencyError) as caught:
        await asyncio.wait_for(
            close_ready_loop_with_cas(
                store,
                snapshot.run_id,
                snapshot.loops[0].loop_instance_id,
                continuation_config=JoinConfig(),
                claim_owner_id="worker-a",
                sleep=sleeper,
            ),
            TEST_TIMEOUT,
        )

    assert caught.value.run_id == snapshot.run_id
    assert store.cas_calls == CAS_MAX_ATTEMPTS + 1
    _assert_paced(sleeper, CAS_MAX_ATTEMPTS)


async def test_loop_reduction_claim_sleeps_between_attempts_but_not_after_the_last() -> None:
    from zeroth.runtime.orchestration.token_lifecycle import CAS_MAX_ATTEMPTS

    snapshot = _loop_with_two_back_edges("run-loop-claim-sleep")
    store = _ContendedStore(snapshot)
    sleeper = _RecordingSleep()

    with pytest.raises(LoopReductionRecoveryError, match="exhausted CAS attempts"):
        await asyncio.wait_for(
            close_ready_loop_with_cas(
                store,
                snapshot.run_id,
                snapshot.loops[0].loop_instance_id,
                continuation_config=JoinConfig(),
                claim_owner_id="worker-a",
                sleep=sleeper,
            ),
            TEST_TIMEOUT,
        )

    assert store.cas_calls == CAS_MAX_ATTEMPTS
    _assert_paced(sleeper, CAS_MAX_ATTEMPTS)


async def test_loop_claim_release_sleeps_between_attempts_but_not_after_the_last() -> None:
    from zeroth.runtime.orchestration.token_lifecycle import CAS_MAX_ATTEMPTS

    snapshot = _loop_with_two_back_edges("run-loop-release-sleep")
    store = _ContendedStore(snapshot, allow_commits=1)
    sleeper = _RecordingSleep()

    # The claim wins on its first attempt, the reducer then fails, and every
    # recorded delay belongs to the release loop that cannot give the claim back.
    with pytest.raises(LoopReductionReleaseError, match="atomically released"):
        await asyncio.wait_for(
            close_ready_loop_with_cas(
                store,
                snapshot.run_id,
                snapshot.loops[0].loop_instance_id,
                continuation_config=JoinConfig(),
                reducer=_crashing_reducer,
                claim_owner_id="worker-a",
                sleep=sleeper,
            ),
            TEST_TIMEOUT,
        )

    assert store.cas_calls == CAS_MAX_ATTEMPTS + 1
    _assert_paced(sleeper, CAS_MAX_ATTEMPTS)


async def test_loop_reduction_reclaim_sleeps_between_attempts_but_not_after_the_last() -> None:
    from zeroth.runtime.orchestration.token_lifecycle import CAS_MAX_ATTEMPTS
    from zeroth.runtime.orchestration.token_loop_claims import _claim_loop_with_cas

    snapshot = _loop_with_single_back_edge("run-loop-reclaim-sleep")
    store = _ContendedStore(snapshot, allow_commits=1)
    # A claimed snapshot is observable when a closer crashes after winning it.
    await _claim_loop_with_cas(
        store,
        snapshot.run_id,
        snapshot.loops[0].loop_instance_id,
        JoinConfig(),
        owner_id="dead-worker",
        max_attempts=1,
    )
    observed = LoopReductionClaim.from_loop(store.snapshot.loops[0])
    sleeper = _RecordingSleep()

    with pytest.raises(LoopReductionClaimChangedError, match="exhausted CAS attempts"):
        await asyncio.wait_for(
            reclaim_abandoned_loop_reduction_with_cas(
                store,
                snapshot.run_id,
                snapshot.loops[0].loop_instance_id,
                observed_claim=observed,
                new_owner_id="recovery-worker",
                sleep=sleeper,
            ),
            TEST_TIMEOUT,
        )

    assert store.cas_calls == CAS_MAX_ATTEMPTS + 1
    _assert_paced(sleeper, CAS_MAX_ATTEMPTS)


async def test_scheduler_transition_sleeps_between_attempts_but_not_after_the_last() -> None:
    # ``apply_token_transition`` spends the same budget the lifecycle adapter
    # does; F-08 is that it spent it without any pacing between attempts.
    from zeroth.runtime.orchestration.token_lifecycle import CAS_MAX_ATTEMPTS

    snapshot = _claimed("run-scheduler-sleep")
    store = _ContendedStore(snapshot)
    sleeper = _RecordingSleep()

    with pytest.raises(TokenSnapshotConcurrencyError) as caught:
        await asyncio.wait_for(
            apply_token_transition(store, snapshot.run_id, pause_snapshot, sleep=sleeper),
            TEST_TIMEOUT,
        )

    assert caught.value.run_id == snapshot.run_id
    assert store.cas_calls == CAS_MAX_ATTEMPTS
    _assert_paced(sleeper, CAS_MAX_ATTEMPTS)


async def test_non_positive_attempt_budget_is_rejected() -> None:
    store = _ContendedStore(_root("run-guard"))

    with pytest.raises(ValueError, match="max_attempts"):
        TokenLifecycleAdapter(store, max_attempts=0)
    with pytest.raises(ValueError, match="max_attempts"):
        TokenRuntimeCoordinator(object(), store, max_attempts=0)
