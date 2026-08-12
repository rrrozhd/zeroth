"""WS-E: per-tenant TTL purge, legal-hold-vs-TTL, and policy resolution."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

import zeroth.governance.retention.models as retention_models
from zeroth.governance.retention.models import RetentionPolicy
from zeroth.platform.primitives import utc_now
from zeroth.platform.storage import NullWorkspaceScopeContext
from tests.retention.conftest import seed_token_snapshot


def test_retention_models_consume_platform_clock_per_instance() -> None:
    assert retention_models.RetentionPolicy.model_fields["created_at"].default_factory is utc_now
    assert retention_models.RetentionPolicy.model_fields["updated_at"].default_factory is utc_now
    assert retention_models.LegalHold.model_fields["created_at"].default_factory is utc_now

    first = retention_models.RetentionPolicy(tenant_id="tenant-1")
    second = retention_models.RetentionPolicy(tenant_id="tenant-2")

    assert first.created_at.tzinfo is UTC
    assert first.created_at is not second.created_at


async def _pii_in_audits(database, ssn: str) -> bool:
    async with database.transaction() as connection:
        rows = await connection.fetch_all("SELECT record_json FROM node_audits", ())
    return any(ssn in (row["record_json"] or "") for row in rows)


async def test_policy_resolution_falls_back_to_system_default(env) -> None:
    # Seeded by migration 008: tenant 'default' exists with keep-forever TTLs.
    resolved = await env.policy_repo_for("tenant-without-policy").resolve()
    assert resolved.tenant_id == "tenant-without-policy"
    assert resolved.audit_ttl_seconds is None  # inherits keep-forever default

    saved = await env.upsert_policy(
        RetentionPolicy(tenant_id="tenant-x", audit_ttl_seconds=30, enabled=True)
    )
    assert saved.audit_ttl_seconds == 30
    assert (await env.policy_repo_for("tenant-x").resolve()).audit_ttl_seconds == 30


async def test_per_tenant_ttl_isolation(env) -> None:
    """Tenant A (30d TTL) is purged; tenant B (90d TTL) is untouched in one cycle."""
    old = datetime.now(UTC) - timedelta(days=60)
    await env.seed_run("run-a", tenant_id="tenant-a", created_at=old, ssn="aaa-11-1111")
    await env.seed_run("run-b", tenant_id="tenant-b", created_at=old, ssn="bbb-22-2222")

    await env.upsert_policy(
        RetentionPolicy(
            tenant_id="tenant-a", audit_ttl_seconds=int(timedelta(days=30).total_seconds())
        )
    )
    await env.upsert_policy(
        RetentionPolicy(
            tenant_id="tenant-b", audit_ttl_seconds=int(timedelta(days=90).total_seconds())
        )
    )

    results_a = await env.service_for("tenant-a").purge_tenant("tenant-a")
    results_b = await env.service_for("tenant-b").purge_tenant("tenant-b")

    assert {r.run_id for r in results_a} == {"run-a"}  # 60d old > 30d TTL -> purged
    assert results_b == []  # 60d old < 90d TTL -> retained

    assert await _pii_in_audits(env.database, "aaa-11-1111") is False
    assert await _pii_in_audits(env.database, "bbb-22-2222") is True


async def test_legal_hold_blocks_ttl_purge(env) -> None:
    old = datetime.now(UTC) - timedelta(days=60)
    await env.seed_run("run-hold", tenant_id="tenant-h", created_at=old, ssn="hhh-33-3333")
    await env.seed_run("run-free", tenant_id="tenant-h", created_at=old, ssn="fff-44-4444")
    await env.upsert_policy(
        RetentionPolicy(
            tenant_id="tenant-h", audit_ttl_seconds=int(timedelta(days=30).total_seconds())
        )
    )
    await env.hold_repo_for("tenant-h").place(run_id="run-hold", reason="legal")

    results = await env.service_for("tenant-h").purge_tenant("tenant-h")

    assert {r.run_id for r in results} == {"run-free"}  # held run excluded
    assert await _pii_in_audits(env.database, "hhh-33-3333") is True  # hold beats TTL
    assert await _pii_in_audits(env.database, "fff-44-4444") is False


async def test_ttl_none_keeps_everything(env) -> None:
    old = datetime.now(UTC) - timedelta(days=3650)
    await env.seed_run("run-keep", tenant_id="tenant-keep", created_at=old, ssn="kkk-55-5555")
    # No policy => resolves to keep-forever default (audit_ttl None).
    assert await env.service_for("tenant-keep").purge_tenant("tenant-keep") == []
    assert await _pii_in_audits(env.database, "kkk-55-5555") is True


async def test_disabled_policy_skips_purge(env) -> None:
    old = datetime.now(UTC) - timedelta(days=60)
    await env.seed_run("run-dis", tenant_id="tenant-dis", created_at=old, ssn="ddd-66-6666")
    await env.upsert_policy(
        RetentionPolicy(tenant_id="tenant-dis", audit_ttl_seconds=1, enabled=False)
    )
    assert await env.service_for("tenant-dis").purge_tenant("tenant-dis") == []
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
    from zeroth.platform.config.settings import RetentionSettings

    with pytest.raises(ValidationError):
        RetentionSettings(default_audit_ttl_seconds=bad_ttl)
    with pytest.raises(ValidationError):
        RetentionSettings(default_run_ttl_seconds=bad_ttl)


def test_settings_worker_poll_interval_stays_float() -> None:
    from zeroth.platform.config.settings import RetentionSettings

    assert RetentionSettings(worker_poll_interval=0.5).worker_poll_interval == 0.5


# --- configured defaults (retention-correctness task 2) ---------------------


async def test_missing_tenant_policy_inherits_configured_defaults(sqlite_db) -> None:
    from zeroth.governance.retention.policy_repository import RetentionPolicyRepository

    repo = RetentionPolicyRepository.scoped(
        sqlite_db,
        NullWorkspaceScopeContext(tenant_id="tenant-without-policy"),
        default_policy=RetentionPolicy(
            tenant_id="default", audit_ttl_seconds=3600, run_ttl_seconds=7200
        ),
    )
    resolved = await repo.resolve()
    assert resolved.tenant_id == "tenant-without-policy"
    assert resolved.audit_ttl_seconds == 3600
    assert resolved.run_ttl_seconds == 7200


async def test_explicit_none_ttl_beats_configured_default(sqlite_db) -> None:
    from zeroth.governance.retention.policy_repository import RetentionPolicyRepository

    repo = RetentionPolicyRepository.scoped(
        sqlite_db,
        NullWorkspaceScopeContext(tenant_id="tenant-forever"),
        default_policy=RetentionPolicy(tenant_id="default", audit_ttl_seconds=3600),
    )
    await repo.upsert(RetentionPolicy(tenant_id="tenant-forever", audit_ttl_seconds=None))
    resolved = await repo.resolve()
    # Explicit NULL = keep forever, even though the configured default is finite.
    assert resolved.audit_ttl_seconds is None


async def test_configured_defaults_are_not_persisted_as_rows(sqlite_db) -> None:
    from zeroth.governance.retention.policy_repository import RetentionPolicyRepository

    repo = RetentionPolicyRepository.scoped(
        sqlite_db,
        NullWorkspaceScopeContext(tenant_id="tenant-ephemeral"),
        default_policy=RetentionPolicy(tenant_id="default", audit_ttl_seconds=3600),
    )
    await repo.resolve()
    assert await repo.get() is None


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
    await env.upsert_policy(
        RetentionPolicy(
            tenant_id="t-audit", audit_ttl_seconds=int(timedelta(days=30).total_seconds())
        )
    )
    checkpoints_before = await _checkpoint_count(env.database, "run-mixed")

    results = await env.service_for("t-audit").purge_audits("t-audit")

    assert len(results) == 1
    assert results[0].run_id == "run-mixed"
    assert results[0].audits_erased == 1
    assert results[0].run_redacted is False
    assert results[0].checkpoints_deleted == 0

    records = {r.audit_id: r for r in await env.audit_repo_for("t-audit").list_by_run("run-mixed")}
    assert records["run-mixed-a0"].erased is True
    assert records["run-mixed-a1"].erased is False
    assert "mmm-77-7777" in str(records["run-mixed-a1"].input_snapshot)
    # Full-surface erasure did NOT happen: run row and checkpoints survive.
    run = await env.run_repo_for("t-audit").get("run-mixed")
    assert "mmm-77-7777" in str(run.final_output)
    assert await _checkpoint_count(env.database, "run-mixed") == checkpoints_before


async def test_audit_ttl_sweep_respects_legal_holds(env) -> None:
    old = datetime.now(UTC) - timedelta(days=60)
    await env.seed_run("run-a-held", tenant_id="t-ah", created_at=old, ssn="hhh-88-8888")
    await env.seed_run("run-a-free", tenant_id="t-ah", created_at=old, ssn="fff-99-9999")
    await env.upsert_policy(
        RetentionPolicy(tenant_id="t-ah", audit_ttl_seconds=int(timedelta(days=30).total_seconds()))
    )
    await env.hold_repo_for("t-ah").place(run_id="run-a-held", reason="litigation")

    results = await env.service_for("t-ah").purge_audits("t-ah")

    assert {r.run_id for r in results} == {"run-a-free"}
    assert await _pii_in_audits(env.database, "hhh-88-8888") is True
    assert await _pii_in_audits(env.database, "fff-99-9999") is False


# --- terminal-run TTL (retention-correctness task 4) -------------------------


async def _force_run_state(database, run_id: str, *, status: str, updated_at: datetime) -> None:
    async with database.transaction() as connection:
        await connection.execute(
            "UPDATE runs SET status = ?, updated_at = ? WHERE run_id = ?",
            (status, updated_at.astimezone(UTC).isoformat(), run_id),
        )


async def test_run_ttl_erases_only_old_terminal_runs(env) -> None:
    old = datetime.now(UTC) - timedelta(days=60)
    ttl = int(timedelta(days=30).total_seconds())
    cases = {
        "run-done": ("COMPLETED", old),
        "run-fail": ("FAILED", old),
        "run-pend": ("PENDING", old),
        "run-live": ("RUNNING", old),
        "run-appr": ("WAITING_APPROVAL", old),
        "run-intr": ("WAITING_INTERRUPT", old),
        "run-new": ("COMPLETED", datetime.now(UTC)),
    }
    for run_id, (status, updated_at) in cases.items():
        await env.seed_run(run_id, tenant_id="t-run", ssn=f"ssn-{run_id}")
        await seed_token_snapshot(
            env,
            run_id,
            artifact_key=f"{run_id}/token/blob",
            ssn=f"ssn-{run_id}",
            tenant_id="t-run",
        )
        await _force_run_state(env.database, run_id, status=status, updated_at=updated_at)
    await env.upsert_policy(RetentionPolicy(tenant_id="t-run", run_ttl_seconds=ttl))

    results = await env.service_for("t-run").purge_runs("t-run")

    assert {r.run_id for r in results} == {"run-done", "run-fail"}
    for run_id in ("run-done", "run-fail"):
        run = await env.run_repo_for("t-run").get(run_id)
        assert f"ssn-{run_id}" not in str(run.final_output)
        assert await _checkpoint_count(env.database, run_id) == 0
        assert await env.run_repo_for("t-run").get_token_snapshot(run_id) is None
        assert await _pii_in_audits(env.database, f"ssn-{run_id}") is False
    for run_id in ("run-pend", "run-live", "run-appr", "run-intr", "run-new"):
        run = await env.run_repo_for("t-run").get(run_id)
        assert f"ssn-{run_id}" in str(run.final_output), run_id
        assert await env.run_repo_for("t-run").get_token_snapshot(run_id) is not None
        assert await _pii_in_audits(env.database, f"ssn-{run_id}") is True


async def test_run_ttl_recheck_blocks_resurrected_run(env) -> None:
    """Barrier: a FAILED run selected for TTL erasure flips to PENDING before
    the destructive transaction — the locked recheck must erase nothing."""
    old = datetime.now(UTC) - timedelta(days=60)
    cutoff = datetime.now(UTC) - timedelta(days=30)
    await env.seed_run("run-barrier", tenant_id="t-bar", ssn="bar-11-1111")
    await _force_run_state(env.database, "run-barrier", status="FAILED", updated_at=old)

    selected = await env.run_repo_for("t-bar").list_erasable_run_ids(cutoff)
    assert selected == ["run-barrier"]

    # The race: a retry resurrects the run between selection and erasure.
    await _force_run_state(env.database, "run-barrier", status="PENDING", updated_at=old)

    result = await env.service_for("t-bar").erase_run(
        "run-barrier", "ttl", tenant_id="t-bar", ttl_cutoff=cutoff
    )
    assert result.audits_erased == 0
    assert result.checkpoints_deleted == 0
    assert result.run_redacted is False
    run = await env.run_repo_for("t-bar").get("run-barrier")
    assert "bar-11-1111" in str(run.final_output)
    assert await _pii_in_audits(env.database, "bar-11-1111") is True


async def test_run_ttl_ignores_runs_with_old_audits_but_recent_activity(env) -> None:
    old = datetime.now(UTC) - timedelta(days=60)
    ttl = int(timedelta(days=30).total_seconds())
    # Audits are old, but the run itself was recently updated (still terminal).
    await env.seed_run("run-active", tenant_id="t-recent", created_at=old, ssn="rec-22-2222")
    await _force_run_state(
        env.database, "run-active", status="COMPLETED", updated_at=datetime.now(UTC)
    )
    await env.upsert_policy(RetentionPolicy(tenant_id="t-recent", run_ttl_seconds=ttl))

    assert await env.service_for("t-recent").purge_runs("t-recent") == []
    run = await env.run_repo_for("t-recent").get("run-active")
    assert "rec-22-2222" in str(run.final_output)


# --- sweep verification (retention-correctness task 6) ------------------------


async def test_signed_chain_verifies_after_audit_ttl_tombstoning(env) -> None:
    from zeroth.governance.audit import AuditContinuityVerifier

    old = datetime.now(UTC) - timedelta(days=60)
    await env.seed_run("run-chain", tenant_id="t-chain", created_at=old, n_audits=3)
    await env.upsert_policy(
        RetentionPolicy(
            tenant_id="t-chain", audit_ttl_seconds=int(timedelta(days=30).total_seconds())
        )
    )

    results = await env.service_for("t-chain").purge_audits("t-chain")
    assert results[0].audits_erased == 3

    report = await AuditContinuityVerifier(
        env.audit_repo_for("t-chain"), signer=env.signer
    ).verify_run("run-chain")
    assert report.verified is True, report.error


async def test_audit_sweep_query_count_does_not_scale_with_records(env, monkeypatch) -> None:
    """Selection is one cutoff-bounded projection query plus one UPDATE per aged
    record — never a per-record list-by-run query."""
    from contextlib import asynccontextmanager

    old = datetime.now(UTC) - timedelta(days=60)
    ttl = int(timedelta(days=30).total_seconds())
    await env.seed_run("run-q3", tenant_id="t-q3", created_at=old, n_audits=3)
    await env.seed_run("run-q8", tenant_id="t-q8", created_at=old, n_audits=8)
    await env.upsert_policy(RetentionPolicy(tenant_id="t-q3", audit_ttl_seconds=ttl))
    await env.upsert_policy(RetentionPolicy(tenant_id="t-q8", audit_ttl_seconds=ttl))

    class _CountingConnection:
        def __init__(self, inner, log: list[str]) -> None:
            self._inner = inner
            self._log = log

        async def fetch_all(self, sql, params=()):  # noqa: ANN001
            self._log.append(sql)
            return await self._inner.fetch_all(sql, params)

        async def fetch_one(self, sql, params=()):  # noqa: ANN001
            self._log.append(sql)
            return await self._inner.fetch_one(sql, params)

        async def execute(self, sql, params=()):  # noqa: ANN001
            self._log.append(sql)
            return await self._inner.execute(sql, params)

        def __getattr__(self, name):  # noqa: ANN001
            return getattr(self._inner, name)

    real_transaction = env.database.transaction
    queries: list[str] = []

    @asynccontextmanager
    async def counting_transaction(**kwargs):  # noqa: ANN003
        async with real_transaction(**kwargs) as connection:
            yield _CountingConnection(connection, queries)

    monkeypatch.setattr(env.database, "transaction", counting_transaction)

    def _sweep_counts() -> tuple[int, int]:
        selects = sum(1 for q in queries if q.lstrip().upper().startswith("SELECT"))
        updates = sum(1 for q in queries if q.lstrip().upper().startswith("UPDATE NODE_AUDITS"))
        return selects, updates

    queries.clear()
    assert (await env.service_for("t-q3").purge_audits("t-q3"))[0].audits_erased == 3
    selects_3, updates_3 = _sweep_counts()

    queries.clear()
    assert (await env.service_for("t-q8").purge_audits("t-q8"))[0].audits_erased == 8
    selects_8, updates_8 = _sweep_counts()

    assert updates_3 == 3
    assert updates_8 == 8
    # The SELECT count is flat: policy + holds + lock row + one projection —
    # NOT one list-by-run query per record.
    assert selects_3 == selects_8
