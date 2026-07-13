"""Async storage for per-tenant retention policies (WS-E)."""

from __future__ import annotations

from datetime import UTC, datetime

from zeroth.core.retention.models import SYSTEM_DEFAULT_TENANT, RetentionPolicy
from zeroth.core.storage import AsyncDatabase


def _to_bool(value: object) -> bool:
    return bool(value)


class RetentionPolicyRepository:
    """CRUD over ``retention_policies`` with system-default fallback."""

    def __init__(self, database: AsyncDatabase) -> None:
        self._database = database

    async def get(self, tenant_id: str) -> RetentionPolicy | None:
        """Return the explicit policy for a tenant, or None if it has none."""
        async with self._database.transaction() as connection:
            row = await connection.fetch_one(
                "SELECT * FROM retention_policies WHERE tenant_id = ?",
                (tenant_id,),
            )
        return None if row is None else self._row_to_policy(row)

    async def resolve(self, tenant_id: str) -> RetentionPolicy:
        """Return the tenant's policy, falling back to the system default.

        When neither the tenant nor the ``'default'`` seed row exists (e.g. a DB
        predating the 008 seed), an all-``None``/keep-forever default is
        synthesized so callers never crash on a missing policy.
        """
        explicit = await self.get(tenant_id)
        if explicit is not None:
            return explicit
        system = await self.get(SYSTEM_DEFAULT_TENANT)
        if system is not None:
            return system.model_copy(update={"tenant_id": tenant_id})
        return RetentionPolicy(tenant_id=tenant_id)

    async def upsert(self, policy: RetentionPolicy) -> RetentionPolicy:
        """Insert or update a tenant's policy; returns the persisted row."""
        now = datetime.now(UTC)
        async with self._database.transaction() as connection:
            existing = await connection.fetch_one(
                "SELECT created_at FROM retention_policies WHERE tenant_id = ?",
                (policy.tenant_id,),
            )
            created_at = existing["created_at"] if existing is not None else now.isoformat()
            await connection.execute(
                """
                INSERT INTO retention_policies
                    (tenant_id, audit_ttl_seconds, run_ttl_seconds, enabled,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id) DO UPDATE SET
                    audit_ttl_seconds = excluded.audit_ttl_seconds,
                    run_ttl_seconds = excluded.run_ttl_seconds,
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """,
                (
                    policy.tenant_id,
                    policy.audit_ttl_seconds,
                    policy.run_ttl_seconds,
                    1 if policy.enabled else 0,
                    created_at,
                    now.isoformat(),
                ),
            )
        resolved = await self.get(policy.tenant_id)
        assert resolved is not None  # noqa: S101 - just written
        return resolved

    async def list_all_enabled(self) -> list[RetentionPolicy]:
        """Return every enabled policy (drives the purge worker's sweep)."""
        async with self._database.transaction() as connection:
            rows = await connection.fetch_all(
                "SELECT * FROM retention_policies WHERE enabled = 1 ORDER BY tenant_id",
            )
        return [self._row_to_policy(row) for row in rows]

    def _row_to_policy(self, row: dict[str, object]) -> RetentionPolicy:
        return RetentionPolicy(
            tenant_id=str(row["tenant_id"]),
            audit_ttl_seconds=row["audit_ttl_seconds"],  # type: ignore[arg-type]
            run_ttl_seconds=row["run_ttl_seconds"],  # type: ignore[arg-type]
            enabled=_to_bool(row["enabled"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )
