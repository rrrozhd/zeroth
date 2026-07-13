"""WS-E: per-tenant TTL purge, legal-hold-vs-TTL, and policy resolution."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from zeroth.core.retention.models import RetentionPolicy


async def _pii_in_audits(database, ssn: str) -> bool:
    async with database.transaction() as connection:
        rows = await connection.fetch_all("SELECT record_json FROM node_audits", ())
    return any(ssn in (row["record_json"] or "") for row in rows)


async def test_policy_resolution_falls_back_to_system_default(env) -> None:
    # Seeded by migration 008: tenant 'default' exists with keep-forever TTLs.
    resolved = await env.policy_repo.resolve("tenant-without-policy")
    assert resolved.tenant_id == "tenant-without-policy"
    assert resolved.audit_ttl_seconds is None  # inherits keep-forever default

    saved = await env.policy_repo.upsert(
        RetentionPolicy(tenant_id="tenant-x", audit_ttl_seconds=30, enabled=True)
    )
    assert saved.audit_ttl_seconds == 30
    assert (await env.policy_repo.resolve("tenant-x")).audit_ttl_seconds == 30


async def test_per_tenant_ttl_isolation(env) -> None:
    """Tenant A (30d TTL) is purged; tenant B (90d TTL) is untouched in one cycle."""
    old = datetime.now(UTC) - timedelta(days=60)
    await env.seed_run("run-a", tenant_id="tenant-a", created_at=old, ssn="aaa-11-1111")
    await env.seed_run("run-b", tenant_id="tenant-b", created_at=old, ssn="bbb-22-2222")

    await env.policy_repo.upsert(
        RetentionPolicy(
            tenant_id="tenant-a", audit_ttl_seconds=int(timedelta(days=30).total_seconds())
        )
    )
    await env.policy_repo.upsert(
        RetentionPolicy(
            tenant_id="tenant-b", audit_ttl_seconds=int(timedelta(days=90).total_seconds())
        )
    )

    results_a = await env.service.purge_tenant("tenant-a")
    results_b = await env.service.purge_tenant("tenant-b")

    assert {r.run_id for r in results_a} == {"run-a"}  # 60d old > 30d TTL -> purged
    assert results_b == []  # 60d old < 90d TTL -> retained

    assert await _pii_in_audits(env.database, "aaa-11-1111") is False
    assert await _pii_in_audits(env.database, "bbb-22-2222") is True


async def test_legal_hold_blocks_ttl_purge(env) -> None:
    old = datetime.now(UTC) - timedelta(days=60)
    await env.seed_run("run-hold", tenant_id="tenant-h", created_at=old, ssn="hhh-33-3333")
    await env.seed_run("run-free", tenant_id="tenant-h", created_at=old, ssn="fff-44-4444")
    await env.policy_repo.upsert(
        RetentionPolicy(
            tenant_id="tenant-h", audit_ttl_seconds=int(timedelta(days=30).total_seconds())
        )
    )
    await env.hold_repo.place("tenant-h", run_id="run-hold", reason="legal")

    results = await env.service.purge_tenant("tenant-h")

    assert {r.run_id for r in results} == {"run-free"}  # held run excluded
    assert await _pii_in_audits(env.database, "hhh-33-3333") is True  # hold beats TTL
    assert await _pii_in_audits(env.database, "fff-44-4444") is False


async def test_ttl_none_keeps_everything(env) -> None:
    old = datetime.now(UTC) - timedelta(days=3650)
    await env.seed_run("run-keep", tenant_id="tenant-keep", created_at=old, ssn="kkk-55-5555")
    # No policy => resolves to keep-forever default (audit_ttl None).
    assert await env.service.purge_tenant("tenant-keep") == []
    assert await _pii_in_audits(env.database, "kkk-55-5555") is True


async def test_disabled_policy_skips_purge(env) -> None:
    old = datetime.now(UTC) - timedelta(days=60)
    await env.seed_run("run-dis", tenant_id="tenant-dis", created_at=old, ssn="ddd-66-6666")
    await env.policy_repo.upsert(
        RetentionPolicy(tenant_id="tenant-dis", audit_ttl_seconds=1, enabled=False)
    )
    assert await env.service.purge_tenant("tenant-dis") == []
    assert await _pii_in_audits(env.database, "ddd-66-6666") is True


# --- TTL input validation (retention-correctness task 1) --------------------


@pytest.mark.parametrize("bad_ttl", [0, -1, -86400])
def test_policy_rejects_non_positive_ttls(bad_ttl: int) -> None:
    with pytest.raises(ValidationError):
        RetentionPolicy(tenant_id="t", audit_ttl_seconds=bad_ttl)
    with pytest.raises(ValidationError):
        RetentionPolicy(tenant_id="t", run_ttl_seconds=bad_ttl)


def test_policy_rejects_fractional_ttls() -> None:
    with pytest.raises(ValidationError):
        RetentionPolicy(tenant_id="t", audit_ttl_seconds=1.5)


@pytest.mark.parametrize("bad_ttl", [0, -1, 1.5])
def test_settings_reject_invalid_default_ttls(bad_ttl: float) -> None:
    from zeroth.core.config.settings import RetentionSettings

    with pytest.raises(ValidationError):
        RetentionSettings(default_audit_ttl_seconds=bad_ttl)
    with pytest.raises(ValidationError):
        RetentionSettings(default_run_ttl_seconds=bad_ttl)


def test_settings_worker_poll_interval_stays_float() -> None:
    from zeroth.core.config.settings import RetentionSettings

    assert RetentionSettings(worker_poll_interval=0.5).worker_poll_interval == 0.5


# --- configured defaults (retention-correctness task 2) ---------------------


async def test_missing_tenant_policy_inherits_configured_defaults(sqlite_db) -> None:
    from zeroth.core.retention.policy_repository import RetentionPolicyRepository

    repo = RetentionPolicyRepository(
        sqlite_db,
        default_policy=RetentionPolicy(
            tenant_id="default", audit_ttl_seconds=3600, run_ttl_seconds=7200
        ),
    )
    resolved = await repo.resolve("tenant-without-policy")
    assert resolved.tenant_id == "tenant-without-policy"
    assert resolved.audit_ttl_seconds == 3600
    assert resolved.run_ttl_seconds == 7200


async def test_explicit_none_ttl_beats_configured_default(sqlite_db) -> None:
    from zeroth.core.retention.policy_repository import RetentionPolicyRepository

    repo = RetentionPolicyRepository(
        sqlite_db,
        default_policy=RetentionPolicy(tenant_id="default", audit_ttl_seconds=3600),
    )
    await repo.upsert(RetentionPolicy(tenant_id="tenant-forever", audit_ttl_seconds=None))
    resolved = await repo.resolve("tenant-forever")
    # Explicit NULL = keep forever, even though the configured default is finite.
    assert resolved.audit_ttl_seconds is None


async def test_configured_defaults_are_not_persisted_as_rows(sqlite_db) -> None:
    from zeroth.core.retention.policy_repository import RetentionPolicyRepository

    repo = RetentionPolicyRepository(
        sqlite_db,
        default_policy=RetentionPolicy(tenant_id="default", audit_ttl_seconds=3600),
    )
    await repo.resolve("tenant-ephemeral")
    assert await repo.get("tenant-ephemeral") is None


# --- audit-TTL tombstoning (retention-correctness task 3) --------------------


async def _checkpoint_count(database, run_id: str) -> int:
    async with database.transaction() as connection:
        row = await connection.fetch_one(
            "SELECT COUNT(*) AS n FROM run_checkpoints WHERE run_id = ?", (run_id,)
        )
    return int(row["n"])


async def test_audit_ttl_tombstones_only_old_audits(env) -> None:
    """One old + one new audit: the sweep erases the old audit's PII and
    leaves the run row, checkpoints, and the new audit fully intact."""
    old = datetime.now(UTC) - timedelta(days=60)
    await env.seed_run("run-mixed", tenant_id="t-audit", n_audits=2, ssn="mmm-77-7777")
    async with env.database.transaction() as connection:
        await connection.execute(
            "UPDATE node_audits SET created_at = ? WHERE audit_id = ?",
            (old.isoformat(), "run-mixed-a0"),
        )
    await env.policy_repo.upsert(
        RetentionPolicy(
            tenant_id="t-audit", audit_ttl_seconds=int(timedelta(days=30).total_seconds())
        )
    )
    checkpoints_before = await _checkpoint_count(env.database, "run-mixed")

    results = await env.service.purge_audits("t-audit")

    assert len(results) == 1
    assert results[0].run_id == "run-mixed"
    assert results[0].audits_erased == 1
    assert results[0].run_redacted is False
    assert results[0].checkpoints_deleted == 0

    records = {r.audit_id: r for r in await env.audit_repo.list_by_run("run-mixed")}
    assert records["run-mixed-a0"].erased is True
    assert records["run-mixed-a1"].erased is False
    assert "mmm-77-7777" in str(records["run-mixed-a1"].input_snapshot)
    # Full-surface erasure did NOT happen: run row and checkpoints survive.
    run = await env.run_repo.get("run-mixed")
    assert "mmm-77-7777" in str(run.final_output)
    assert await _checkpoint_count(env.database, "run-mixed") == checkpoints_before


async def test_audit_ttl_sweep_respects_legal_holds(env) -> None:
    old = datetime.now(UTC) - timedelta(days=60)
    await env.seed_run("run-a-held", tenant_id="t-ah", created_at=old, ssn="hhh-88-8888")
    await env.seed_run("run-a-free", tenant_id="t-ah", created_at=old, ssn="fff-99-9999")
    await env.policy_repo.upsert(
        RetentionPolicy(
            tenant_id="t-ah", audit_ttl_seconds=int(timedelta(days=30).total_seconds())
        )
    )
    await env.hold_repo.place("t-ah", run_id="run-a-held", reason="litigation")

    results = await env.service.purge_audits("t-ah")

    assert {r.run_id for r in results} == {"run-a-free"}
    assert await _pii_in_audits(env.database, "hhh-88-8888") is True
    assert await _pii_in_audits(env.database, "fff-99-9999") is False
