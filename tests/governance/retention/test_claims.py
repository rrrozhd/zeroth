"""Claim coordination: leases, fencing, and the CAS writes behind them.

Every mutation an external-cleanup worker makes is fenced on ``(claim_id,
generation)``. If that fence is wrong, two workers erase the same run
concurrently and one of them writes progress the other has already superseded.

These tests drive the collaborator directly with injected repositories rather
than through the service, so a fencing regression is attributed here rather
than showing up as a confusing failure three layers up.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from zeroth.core.retention.cleanup_manifest import CleanupOperation, operation_id
from zeroth.core.retention.cleanup_state_repository import CleanupStateRepository
from zeroth.core.retention.coordination import RetentionCoordinator
from zeroth.governance.retention.claims import CleanupClaims
from zeroth.governance.retention.errors import StaleCleanupClaimError
from zeroth.governance.retention.replay import CleanupReplayState, replay_cleanup_state


@pytest.fixture
def claims(env) -> CleanupClaims:
    """A collaborator wired to the same database as the service under test."""
    return CleanupClaims(
        coordinator=RetentionCoordinator(env.database),
        log=env.log_repo,
        cleanup_state=CleanupStateRepository(),
        lease_seconds=30.0,
        replay=replay_cleanup_state,
    )


def _state(**overrides) -> CleanupReplayState:
    fields = {"manifest": None, "generation": 1, "active_claim_id": "claim-a"}
    fields.update(overrides)
    return CleanupReplayState(**fields)


def test_the_owning_claim_passes_the_fence() -> None:
    CleanupClaims.verify_active(_state(), "claim-a", 1)


@pytest.mark.parametrize(
    ("claim_id", "generation"),
    [
        ("claim-b", 1),  # another worker holds it
        ("claim-a", 2),  # right worker, superseded generation
        ("claim-b", 2),  # neither matches
    ],
)
def test_a_claim_that_no_longer_owns_the_state_is_rejected(claim_id, generation) -> None:
    with pytest.raises(StaleCleanupClaimError):
        CleanupClaims.verify_active(_state(), claim_id, generation)


def test_a_released_claim_is_rejected() -> None:
    with pytest.raises(StaleCleanupClaimError):
        CleanupClaims.verify_active(_state(active_claim_id=None), "claim-a", 1)


async def test_the_injected_replay_is_what_materializes_a_legacy_row(env, claims) -> None:
    """Replay is injected, not imported, so the service's seam stays patchable."""
    await env.seed_run("run-inject", n_audits=1)
    result = await env.service.erase_run("run-inject", "rte")
    log_id = result.authorization_log_id

    # Drop the materialized row so the next load has to fall back to replay.
    async with env.database.transaction() as connection:
        await connection.execute(
            "DELETE FROM retention_cleanup_state WHERE authorization_log_id = ?",
            (log_id,),
        )
        await connection.execute(
            "DELETE FROM retention_cleanup_operations WHERE authorization_log_id = ?",
            (log_id,),
        )

    calls = []

    def recording_replay(authorization, entries):
        calls.append(str(authorization["log_id"]))
        return replay_cleanup_state(authorization, entries)

    injected = CleanupClaims(
        coordinator=RetentionCoordinator(env.database),
        log=env.log_repo,
        cleanup_state=CleanupStateRepository(),
        lease_seconds=30.0,
        replay=recording_replay,
    )
    authorization = await env.log_repo.get(log_id)
    async with env.database.transaction() as connection:
        state = await injected.load_or_materialize(connection, authorization)

    assert calls == [log_id]
    assert state.terminal_status == "completed"


async def test_a_materialized_row_is_read_without_replaying_the_log(env, claims) -> None:
    await env.seed_run("run-material", n_audits=1)
    result = await env.service.erase_run("run-material", "rte")

    def exploding_replay(authorization, entries):
        raise AssertionError("replay must not run when the state row exists")

    injected = CleanupClaims(
        coordinator=RetentionCoordinator(env.database),
        log=env.log_repo,
        cleanup_state=CleanupStateRepository(),
        lease_seconds=30.0,
        replay=exploding_replay,
    )
    authorization = await env.log_repo.get(result.authorization_log_id)
    async with env.database.transaction() as connection:
        state = await injected.load_or_materialize(connection, authorization)

    assert state.terminal_status == "completed"
    assert state.active_claim_id is None


async def _authorize(env) -> tuple[str, str, CleanupOperation]:
    """Erase a run, then re-open its cleanup with a fresh claim we control."""
    await env.seed_run("run-fence", n_audits=1, artifact_key="run-fence/n0/blob")
    result = await env.service.erase_run("run-fence", "rte")
    log_id = result.authorization_log_id
    operation = CleanupOperation(
        operation_id=operation_id("default", "run-fence", "artifact_key", "run-fence/n0/blob"),
        kind="artifact_key",
        tenant_id="default",
        run_id="run-fence",
        artifact_key="run-fence/n0/blob",
        status="completed",
        deleted_count=1,
    )
    return log_id, "default", operation


async def test_a_heartbeat_from_a_stale_claim_is_refused(env, claims) -> None:
    log_id, tenant, _ = await _authorize(env)

    with pytest.raises(StaleCleanupClaimError):
        await claims.record_heartbeat(
            authorization_log_id=log_id,
            claim_id="never-held",
            generation=99,
            tenant_id=tenant,
            run_id="run-fence",
        )


async def test_an_operation_delta_from_a_stale_claim_is_refused(env, claims) -> None:
    log_id, _, operation = await _authorize(env)

    with pytest.raises(StaleCleanupClaimError):
        await claims.record_operation_delta(log_id, "never-held", 99, operation)


async def test_a_terminal_write_from_a_stale_claim_is_refused(env, claims) -> None:
    log_id, _, _ = await _authorize(env)
    manifest_state = await env.log_repo.get(log_id)
    assert manifest_state is not None

    async with env.database.transaction() as connection:
        state = await claims.load_or_materialize(connection, manifest_state)

    with pytest.raises(StaleCleanupClaimError):
        await claims.record_terminal(
            log_id,
            "never-held",
            99,
            state.manifest,
            failed=False,
        )


async def test_releasing_a_claim_that_is_not_held_changes_nothing(env, claims) -> None:
    """Release is a best-effort cleanup, so a stale caller must be a silent no-op."""
    log_id, tenant, _ = await _authorize(env)
    before = await env.log_repo.list_for_run("run-fence")

    await claims.release(
        authorization_log_id=log_id,
        claim_id="never-held",
        generation=99,
        tenant_id=tenant,
        run_id="run-fence",
        reason="rte",
    )

    after = await env.log_repo.list_for_run("run-fence")
    assert len(after) == len(before)
    assert not any(row["action"] == "external_cleanup_claim_released" for row in after)


async def test_a_heartbeat_extends_the_lease_it_holds(env, claims) -> None:
    """The lease has to move forward, or a slow operation loses its own claim."""
    await env.seed_run("run-lease", n_audits=1)
    result = await env.service.erase_run("run-lease", "rte")
    log_id = result.authorization_log_id

    # Re-open the cleanup by planting an active claim we own.
    lease = datetime.now(UTC) + timedelta(seconds=5)
    async with env.database.transaction() as connection:
        await connection.execute(
            """
            UPDATE retention_cleanup_state
            SET active_claim_id = ?, active_claim_log_id = ?, generation = ?,
                lease_expires_at = ?, terminal_status = NULL, terminal_log_id = NULL
            WHERE authorization_log_id = ?
            """,
            ("claim-live", "claim-live-log", 7, lease.isoformat(), log_id),
        )

    await claims.record_heartbeat(
        authorization_log_id=log_id,
        claim_id="claim-live",
        generation=7,
        tenant_id="default",
        run_id="run-lease",
    )

    async with env.database.transaction() as connection:
        row = await connection.fetch_one(
            "SELECT lease_expires_at FROM retention_cleanup_state WHERE authorization_log_id = ?",
            (log_id,),
        )
    assert datetime.fromisoformat(row["lease_expires_at"]) > lease
