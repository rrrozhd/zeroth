"""Background retention purge worker (WS-E).

Mirrors :class:`zeroth.governance.approvals.sla_checker.ApprovalSLAChecker` exactly: an
``@dataclass`` with an ``async poll_loop`` that runs until cancelled, catches and
logs per-iteration errors, and sleeps ``poll_interval`` seconds between sweeps.
Started in the service lifespan via ``asyncio.create_task`` and cancelled on
shutdown.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from zeroth.governance.retention.policy_repository import EnabledPolicyMaintenanceReader
from zeroth.platform.storage import NullWorkspaceScopeContext, ScopeContext

if TYPE_CHECKING:
    # Annotation-only: importing the service here would put it on this package's
    # own import path, and its collaborators import back into this package.
    from zeroth.governance.retention.erasure_service import RetentionErasureService

logger = logging.getLogger(__name__)


class WorkspaceMaintenanceReader(Protocol):
    async def list_workspace_ids(self) -> list[str]: ...


@dataclass
class RetentionPurgeWorker:
    """Periodically purges aged, non-held runs per each tenant's TTL policy.

    Attributes:
        erasure_service: Performs the actual per-run erasure.
        policy_repository: Enumerates enabled tenant policies to sweep.
        poll_interval: Seconds between purge sweeps (default 3600).
    """

    policy_reader: EnabledPolicyMaintenanceReader
    workspace_reader_factory: Callable[[str], WorkspaceMaintenanceReader]
    erasure_service_factory: Callable[
        [ScopeContext | NullWorkspaceScopeContext], RetentionErasureService
    ]
    poll_interval: float = 3600.0

    async def sweep_once(self) -> None:
        """Enumerate policies, then sweep through tenant-bound collaborators."""
        policies = await self.policy_reader.list_all_enabled_for_maintenance()
        for policy in policies:
            workspace_ids = await self.workspace_reader_factory(
                policy.tenant_id
            ).list_workspace_ids()
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
                service = self.erasure_service_factory(scope)
                for sweep in (service.purge_runs, service.purge_audits):
                    try:
                        await sweep(policy.tenant_id)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception(
                            "retention %s failed for tenant %s",
                            sweep.__name__,
                            policy.tenant_id,
                        )

    async def poll_loop(self) -> None:
        """Continuously sweep each tenant's TTL surfaces until cancelled.

        Run erasure (``purge_runs``) and audit tombstoning (``purge_audits``)
        are independent sweeps: a failure in one is logged and does not abort
        the other surface, the next tenant, or the next tick.
        """
        while True:
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("retention purge worker poll error")
            await asyncio.sleep(self.poll_interval)
