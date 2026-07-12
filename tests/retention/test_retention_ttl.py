"""WS-E: per-tenant TTL purge, legal-hold-vs-TTL, and policy resolution."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
