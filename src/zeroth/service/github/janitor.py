"""Background maintenance for the GitHub integration surface.

Two duties: pruning the webhook delivery dedup ledger after its retention
window, and -- when the ZER-37 repository surface is wired -- sweeping STAGED
checkouts past their expiry horizon to EXPIRED and deleting the staging
directories of EXPIRED/FAILED checkouts. The worker follows the retention
purge worker's shape (an async poll loop started and cancelled by the service
lifespan). Staging-directory deletion is confined to the configured staging
root: a persisted path that resolves outside it is left alone rather than
deleted, so a corrupted row can never aim the janitor at the host filesystem.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import timedelta
from pathlib import Path

from zeroth.platform.primitives import utc_now
from zeroth.service.github.repository import SQLiteGitHubRepository
from zeroth.service.repositories.repo_models import RepoCheckoutState
from zeroth.service.repositories.repository import SQLiteRepoCheckoutRepository

logger = logging.getLogger(__name__)

_DEFAULT_POLL_INTERVAL_SECONDS = 3600.0
_DEFAULT_DELIVERY_RETENTION = timedelta(days=7)

# Terminal checkout states whose staging directories have no further use.
_SWEPT_CHECKOUT_STATES = (RepoCheckoutState.EXPIRED, RepoCheckoutState.FAILED)


class GitHubMaintenanceWorker:
    """Periodic sweeper over the GitHub integration's persisted state."""

    def __init__(
        self,
        repository: SQLiteGitHubRepository,
        *,
        tenant_id: str = "default",
        poll_interval: float = _DEFAULT_POLL_INTERVAL_SECONDS,
        delivery_retention: timedelta = _DEFAULT_DELIVERY_RETENTION,
        checkout_repository: SQLiteRepoCheckoutRepository | None = None,
        staging_root: Path | None = None,
        workspace_id: str | None = None,
    ) -> None:
        self._repository = repository
        self._tenant_id = tenant_id
        self._poll_interval = poll_interval
        self._delivery_retention = delivery_retention
        self._checkout_repository = checkout_repository
        self._staging_root = Path(staging_root) if staging_root is not None else None
        self._workspace_id = workspace_id

    @property
    def poll_interval(self) -> float:
        """Seconds between maintenance sweeps."""
        return self._poll_interval

    async def sweep_once(self) -> int:
        """Run one maintenance sweep; returns the number of pruned deliveries."""
        cutoff = utc_now() - self._delivery_retention
        pruned = await self._repository.prune_deliveries(self._tenant_id, cutoff)
        if pruned:
            logger.info("pruned %d expired github webhook delivery record(s)", pruned)
        await self._sweep_checkouts()
        return pruned

    async def _sweep_checkouts(self) -> None:
        """Expire stale STAGED checkouts and drop dead staging directories."""
        if self._checkout_repository is None:
            return
        expired = await self._checkout_repository.expire_stale(
            self._tenant_id, workspace_id=self._workspace_id
        )
        if expired:
            logger.info("expired %d stale repo checkout(s)", len(expired))
        if self._staging_root is None:
            return
        root = self._staging_root.resolve()
        for state in _SWEPT_CHECKOUT_STATES:
            checkouts = await self._checkout_repository.list_checkouts(
                self._tenant_id, workspace_id=self._workspace_id, state=state
            )
            for checkout in checkouts:
                self._remove_staging_dir(root, checkout.staged_path)

    @staticmethod
    def _remove_staging_dir(root: Path, staged_path: str | None) -> None:
        """Delete one staging directory, but only when it lives under the root."""
        if not staged_path:
            return
        candidate = Path(staged_path)
        try:
            resolved = candidate.resolve()
        except OSError:
            return
        if resolved == root or not resolved.is_relative_to(root):
            return
        shutil.rmtree(candidate, ignore_errors=True)

    async def poll_loop(self) -> None:
        """Sweep on the poll interval until cancelled by the service lifespan."""
        while True:
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # The sweep is best-effort hygiene; a failed pass must not end
                # the loop. Only the exception type could carry foreign text,
                # so the traceback is safe to log for the operator.
                logger.exception("github maintenance sweep failed")
            await asyncio.sleep(self._poll_interval)


__all__ = ["GitHubMaintenanceWorker"]
