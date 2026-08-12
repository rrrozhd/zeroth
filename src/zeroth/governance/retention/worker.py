"""Background retention purge worker."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from zeroth.governance.retention.models import RetentionPolicy
from zeroth.platform.storage import NullWorkspaceScopeContext, ScopeContext

if TYPE_CHECKING:
    from zeroth.governance.retention.erasure_service import RetentionErasureService
    from zeroth.governance.retention.policy_repository import RetentionPolicyRepository

logger = logging.getLogger(__name__)


class _PolicyReader(Protocol):
    async def list_all_enabled_for_maintenance(self) -> list[RetentionPolicy]: ...


class _TenantReader(Protocol):
    async def list_tenant_ids(self) -> list[str]: ...


class _WorkspaceReader(Protocol):
    async def list_workspace_ids(self) -> list[str]: ...


class _PolicyRepository(Protocol):
    @property
    def tenant_id(self) -> str: ...

    async def resolve(self) -> RetentionPolicy: ...


class _ErasureService(Protocol):
    async def purge_runs(self, tenant_id: str) -> list[object]: ...

    async def purge_audits(self, tenant_id: str) -> list[object]: ...


class RetentionPurgeWorker:
    """Sweep effective policies through exact tenant/workspace collaborators."""

    def __init__(
        self,
        erasure_service: RetentionErasureService,
        policy_repository: RetentionPolicyRepository,
        poll_interval: float = 3600.0,
    ) -> None:
        self.erasure_service = erasure_service
        self.policy_repository = policy_repository
        self.policy_reader = None
        self.tenant_reader = None
        self.policy_repository_factory = None
        self.workspace_reader_factory = None
        self.erasure_service_factory = None
        self.poll_interval = poll_interval

    @classmethod
    def for_shared_database(
        cls,
        *,
        policy_reader: _PolicyReader,
        workspace_reader_factory: Callable[[str], _WorkspaceReader],
        erasure_service_factory: Callable[
            [ScopeContext | NullWorkspaceScopeContext], _ErasureService
        ],
        tenant_reader: _TenantReader | None = None,
        policy_repository_factory: Callable[[str], _PolicyRepository] | None = None,
        poll_interval: float = 3600.0,
    ) -> RetentionPurgeWorker:
        worker = cls.__new__(cls)
        worker.erasure_service = None
        worker.policy_repository = None
        worker.policy_reader = policy_reader
        worker.tenant_reader = tenant_reader
        worker.policy_repository_factory = policy_repository_factory
        worker.workspace_reader_factory = workspace_reader_factory
        worker.erasure_service_factory = erasure_service_factory
        worker.poll_interval = poll_interval
        return worker

    async def sweep_once(self) -> None:
        if self.policy_reader is None:
            await self._sweep_owner()
        else:
            await self._sweep_shared()

    async def _sweep_owner(self) -> None:
        policy = await self.policy_repository.resolve()
        if policy.tenant_id != self.policy_repository.tenant_id:
            raise ValueError("retention policy does not match bound tenant")
        if policy.enabled:
            await self._sweep_service(self.erasure_service, policy.tenant_id)

    async def _sweep_shared(self) -> None:
        explicit = await self.policy_reader.list_all_enabled_for_maintenance()
        explicit_by_tenant = {policy.tenant_id: policy for policy in explicit}
        tenant_ids = set(explicit_by_tenant)
        if self.tenant_reader is not None:
            tenant_ids.update(await self.tenant_reader.list_tenant_ids())
        for tenant_id in sorted(tenant_ids):
            try:
                policy = explicit_by_tenant.get(tenant_id)
                if self.policy_repository_factory is not None:
                    policy = await self.policy_repository_factory(tenant_id).resolve()
                assert policy is not None
                if not policy.enabled:
                    continue
                workspace_ids = await self.workspace_reader_factory(tenant_id).list_workspace_ids()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("retention discovery failed for tenant %s", tenant_id)
                continue
            null_scope = (
                NullWorkspaceScopeContext.for_default_compatibility()
                if tenant_id == "default"
                else NullWorkspaceScopeContext(tenant_id=tenant_id)
            )
            scopes: list[ScopeContext | NullWorkspaceScopeContext] = [null_scope]
            scopes.extend(
                ScopeContext.for_default_compatibility(workspace_id=workspace_id)
                if tenant_id == "default"
                else ScopeContext(tenant_id=tenant_id, workspace_id=workspace_id)
                for workspace_id in workspace_ids
            )
            for scope in scopes:
                try:
                    service = self.erasure_service_factory(scope)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "retention service construction failed for tenant %s", tenant_id
                    )
                    continue
                await self._sweep_service(service, tenant_id)

    @staticmethod
    async def _sweep_service(service: _ErasureService, tenant_id: str) -> None:
        for sweep in (service.purge_runs, service.purge_audits):
            try:
                await sweep(tenant_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("retention %s failed for tenant %s", sweep.__name__, tenant_id)

    async def poll_loop(self) -> None:
        while True:
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("retention purge worker poll error")
            await asyncio.sleep(self.poll_interval)


# Preserve the established public constructor surface under postponed annotations.
RetentionPurgeWorker.__init__.__annotations__["return"] = None
