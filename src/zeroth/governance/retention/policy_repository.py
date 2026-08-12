"""Async storage for per-tenant retention policies (WS-E)."""

from __future__ import annotations

from datetime import UTC, datetime

from zeroth.governance.retention.models import RetentionPolicy
from zeroth.platform.storage import (
    SERVICE_SCOPE_REGISTRY,
    AsyncDatabase,
    NullWorkspaceScopeContext,
    ScopedTable,
)
from zeroth.platform.storage.scoping import ResourceOperation, persistence_operation


def _to_bool(value: object) -> bool:
    return bool(value)


class RetentionPolicyRepository:
    """CRUD over ``retention_policies`` with system-default fallback.

    ``default_policy`` carries the operator's configured defaults
    (``settings.retention.default_*``). It participates only in
    :meth:`resolve` fallback — it is never persisted, so environment
    configuration cannot masquerade as an explicit tenant policy row.
    """

    def __init__(
        self,
        database: AsyncDatabase,
        scope_context: NullWorkspaceScopeContext,
        *,
        default_policy: RetentionPolicy | None = None,
    ) -> None:
        self._database = database
        if type(scope_context) is NullWorkspaceScopeContext:
            self._policies = ScopedTable(
                database, SERVICE_SCOPE_REGISTRY, "service.retention_policies", scope_context
            )
        else:
            raise TypeError("scope_context must be a trusted tenant scope")
        self._scope_context = scope_context
        self._system_policies = ScopedTable(
            database,
            SERVICE_SCOPE_REGISTRY,
            "service.retention_policies",
            NullWorkspaceScopeContext.for_default_compatibility(),
        )
        self._default_policy = default_policy

    @classmethod
    def for_default_compatibility(
        cls,
        database: AsyncDatabase,
        *,
        default_policy: RetentionPolicy | None = None,
    ) -> RetentionPolicyRepository:
        return cls(
            database,
            NullWorkspaceScopeContext.for_default_compatibility(),
            default_policy=default_policy,
        )

    @property
    def tenant_id(self) -> str:
        """Tenant structurally bound to policy operations."""
        return self._scope_context.tenant_id

    @persistence_operation(ResourceOperation.READ)
    async def get(self) -> RetentionPolicy | None:
        """Return the explicit policy for a tenant, or None if it has none."""
        row = await self._policies.select_one(where={})
        return None if row is None else self._row_to_policy(row)

    @persistence_operation(ResourceOperation.READ)
    async def resolve(self) -> RetentionPolicy:
        """Return the tenant's policy, falling back through the defaults chain.

        Order: explicit tenant row (a stored ``NULL`` TTL means keep forever,
        even when a finite default is configured) → the constructor's
        configured default (environment-derived; the migration-008 seed row is
        all-``NULL``, so configuration must outrank it to ever take effect) →
        the ``'default'`` system row → a synthesized keep-forever policy.
        """
        tenant_id = self._scope_context.tenant_id
        explicit = await self.get()
        if explicit is not None and (tenant_id != "default" or self._default_policy is None):
            return explicit
        if self._default_policy is not None:
            return self._default_policy.model_copy(update={"tenant_id": tenant_id})
        if explicit is not None:
            return explicit
        system_row = await self._system_policies.select_one(where={})
        system = None if system_row is None else self._row_to_policy(system_row)
        if system is not None:
            return system.model_copy(update={"tenant_id": tenant_id})
        return RetentionPolicy(tenant_id=tenant_id)

    @persistence_operation(
        ResourceOperation.CREATE, ResourceOperation.READ, ResourceOperation.UPDATE
    )
    async def upsert(self, policy: RetentionPolicy) -> RetentionPolicy:
        """Insert or update a tenant's policy; returns the persisted row."""
        now = datetime.now(UTC)
        if policy.tenant_id != self._scope_context.tenant_id:
            raise ValueError("tenant_id does not match bound scope")
        async with self._policies.transaction(write_lock=True) as policies:
            existing = await policies.select_one(where={}, columns=("created_at",))
            created_at = existing["created_at"] if existing is not None else now.isoformat()
            values = {
                "audit_ttl_seconds": policy.audit_ttl_seconds,
                "run_ttl_seconds": policy.run_ttl_seconds,
                "enabled": 1 if policy.enabled else 0,
                "created_at": created_at,
                "updated_at": now.isoformat(),
            }
            if existing is None:
                await policies.insert(values)
            else:
                await policies.update(
                    {key: value for key, value in values.items() if key != "created_at"},
                    where={"tenant_id": policy.tenant_id},
                )
        resolved = await self.get()
        assert resolved is not None  # noqa: S101 - just written
        return resolved

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def list_for_tenant(self) -> list[RetentionPolicy]:
        """Return the explicit policy in this repository's bound tenant."""
        async with self._policies.transaction() as policies:
            rows = await policies.select(order_by=("tenant_id",))
        return [self._row_to_policy(row) for row in rows]

    @staticmethod
    def _row_to_policy(row: dict[str, object]) -> RetentionPolicy:
        return RetentionPolicy(
            tenant_id=str(row["tenant_id"]),
            audit_ttl_seconds=row["audit_ttl_seconds"],  # type: ignore[arg-type]
            run_ttl_seconds=row["run_ttl_seconds"],  # type: ignore[arg-type]
            enabled=_to_bool(row["enabled"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )


class EnabledPolicyMaintenanceReader:
    """The complete read-only surface for scheduled cross-tenant policy discovery."""

    def __init__(self, database: AsyncDatabase) -> None:
        from zeroth.platform.storage import CrossTenantMaintenanceScopeContext

        self._policies = ScopedTable.for_cross_tenant_maintenance(
            database,
            SERVICE_SCOPE_REGISTRY,
            "service.retention_policies",
            CrossTenantMaintenanceScopeContext.for_scheduled_maintenance(),
        )

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def list_all_enabled_for_maintenance(self) -> list[RetentionPolicy]:
        async with self._policies.transaction() as policies:
            rows = await policies.select(where={"enabled": 1}, order_by=("tenant_id",))
        return [RetentionPolicyRepository._row_to_policy(row) for row in rows]
