"""Background retention purge worker."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Protocol

from zeroth.governance.retention.models import RetentionPolicy
from zeroth.platform.storage import NullWorkspaceScopeContext, ScopeContext

logger = logging.getLogger(__name__)


class _PolicyReader(Protocol):
    async def list_all_enabled_for_maintenance(self) -> list[RetentionPolicy]: ...


class _WorkspaceReader(Protocol):
    async def list_workspace_ids(self) -> list[str]: ...


class _ErasureService(Protocol):
    async def purge_runs(self, tenant_id: str) -> list[object]: ...
    async def purge_audits(self, tenant_id: str) -> list[object]: ...


class _OwnerPolicyRepository(Protocol):
    @property
    def tenant_id(self) -> str: ...
    async def resolve(self) -> RetentionPolicy: ...


class RetentionPurgeWorker:
    """Sweep enabled policies through exact per-tenant/workspace collaborators.

    The positional owner-local constructor remains supported for public
    compatibility. Production shared-database scheduling is constructed via
    :meth:`for_shared_database` so its broader read-only discovery is explicit.
    """

    erasure_service: _ErasureService | None
    policy_repository: _OwnerPolicyRepository | None
    policy_reader: _PolicyReader | None
    workspace_reader_factory: Callable[[str], _WorkspaceReader] | None
    erasure_service_factory: (
        Callable[[ScopeContext | NullWorkspaceScopeContext], _ErasureService] | None
    )
    poll_interval: float

    def __init__(
        self,
        erasure_service: _ErasureService,
        policy_repository: _OwnerPolicyRepository,
        poll_interval: float = 3600.0,
    ) -> None:
        self.erasure_service = erasure_service
        self.policy_repository = policy_repository
        self.policy_reader: _PolicyReader | None = None
        self.workspace_reader_factory: Callable[[str], _WorkspaceReader] | None = None
        self.erasure_service_factory: (
            Callable[[ScopeContext | NullWorkspaceScopeContext], _ErasureService] | None
        ) = None
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
        poll_interval: float = 3600.0,
    ) -> RetentionPurgeWorker:
        worker = cls.__new__(cls)
        worker.erasure_service = None
        worker.policy_repository = None
        worker.policy_reader = policy_reader
        worker.workspace_reader_factory = workspace_reader_factory
        worker.erasure_service_factory = erasure_service_factory
        worker.poll_interval = poll_interval
        return worker

    async def sweep_once(self) -> None:
        if self.policy_reader is None:
            await self._sweep_owner()
            return
        await self._sweep_shared()

    async def _sweep_owner(self) -> None:
        assert self.policy_repository is not None
        assert self.erasure_service is not None
        tenant_id = self.policy_repository.tenant_id
        policy = await self.policy_repository.resolve()
        if policy.tenant_id != tenant_id:
            raise ValueError("retention policy does not match bound tenant")
        if policy.enabled:
            await self._sweep_service(self.erasure_service, tenant_id)

    async def _sweep_shared(self) -> None:
        assert self.workspace_reader_factory is not None
        assert self.erasure_service_factory is not None
        policies = await self.policy_reader.list_all_enabled_for_maintenance()
        for policy in policies:
            try:
                workspace_ids = await self.workspace_reader_factory(
                    policy.tenant_id
                ).list_workspace_ids()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "retention workspace discovery failed for tenant %s", policy.tenant_id
                )
                continue
            null_scope = (
                NullWorkspaceScopeContext.for_default_compatibility()
                if policy.tenant_id == "default"
                else NullWorkspaceScopeContext(tenant_id=policy.tenant_id)
            )
            scopes: list[ScopeContext | NullWorkspaceScopeContext] = [null_scope]
            scopes.extend(
                ScopeContext.for_default_compatibility(workspace_id=workspace_id)
                if policy.tenant_id == "default"
                else ScopeContext(tenant_id=policy.tenant_id, workspace_id=workspace_id)
                for workspace_id in workspace_ids
            )
            for scope in scopes:
                try:
                    service = self.erasure_service_factory(scope)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "retention service construction failed for tenant %s workspace %s",
                        policy.tenant_id,
                        getattr(scope, "workspace_id", None),
                    )
                    continue
                await self._sweep_service(service, policy.tenant_id)

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
