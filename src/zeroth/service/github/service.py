"""Tenant-facing lifecycle service over the GitHub App integration.

Owns the claim / refresh / revoke lifecycle of installations and keeps the
persisted repository grants in sync with GitHub's live answer. Network access
goes through :class:`GitHubAppClient` and the governed HTTP factory only; the
one request this module performs itself is the installation-wide token mint
that the client's repo-scoped surface cannot express (enumerating an
installation's repositories requires a token that is not restricted to a
single repository). That token is used for one listing, then best-effort
revoked, and is never logged or cached.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx

from zeroth.integrations.github.client import GitHubAppClient
from zeroth.integrations.github.models import (
    CheckoutError,
    CheckoutFailureCode,
    GitHubApiError,
    InstallationRevokedError,
    InstallationState,
    InstallationSuspendedError,
    RepositoryState,
)
from zeroth.integrations.github.token_broker import InstallationTokenBroker
from zeroth.integrations.http.factory import governed_async_client
from zeroth.platform.primitives import utc_now
from zeroth.service.github.repository import (
    GitHubInstallationRecord,
    GitHubRepositoryRecord,
    SQLiteGitHubRepository,
)

if TYPE_CHECKING:
    from zeroth.integrations.github.app_jwt import AppJwtIssuer
    from zeroth.integrations.github.config import GitHubAppConfig

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 30.0


class GitHubIntegrationService:
    """Claim, refresh, list, and revoke GitHub App installations for one tenant."""

    def __init__(
        self,
        repository: SQLiteGitHubRepository,
        client: GitHubAppClient,
        broker: InstallationTokenBroker,
        *,
        config: GitHubAppConfig,
        jwt_issuer: AppJwtIssuer,
        tenant_id: str = "default",
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._repository = repository
        self._client = client
        self._broker = broker
        self._config = config
        self._jwt_issuer = jwt_issuer
        self._tenant_id = tenant_id
        self._transport = transport
        self._timeout_seconds = timeout_seconds

    @property
    def tenant_id(self) -> str:
        """The deployment tenant this service (and its webhook receiver) serves."""
        return self._tenant_id

    @property
    def repository(self) -> SQLiteGitHubRepository:
        """The persistence surface, for the webhook receiver's dispatch."""
        return self._repository

    # -- lifecycle -------------------------------------------------------------

    async def claim_installation(
        self, tenant_id: str, installation_id: int
    ) -> GitHubInstallationRecord:
        """Attach one installation to a tenant after verifying it live.

        A 404 from GitHub marks any persisted row REVOKED (with the repository
        cascade) and re-raises :class:`InstallationRevokedError`; a live answer
        upserts the row ACTIVE (or SUSPENDED) and refreshes the repository
        grants from GitHub's listing.
        """
        return await self._sync_installation(tenant_id, installation_id)

    async def refresh_installation(
        self, tenant_id: str, installation_id: int
    ) -> GitHubInstallationRecord:
        """Re-verify one installation live and re-sync its repository grants."""
        return await self._sync_installation(tenant_id, installation_id)

    async def list_installations(self, tenant_id: str) -> list[GitHubInstallationRecord]:
        """Return the tenant's installations, live-verifying the ACTIVE ones.

        Verification goes through the broker's memoized
        :meth:`InstallationTokenBroker.verify_installation`; a revocation seen
        here cascades exactly as a webhook-delivered one would.
        """
        records = await self._repository.list_installations(tenant_id)
        refreshed: list[GitHubInstallationRecord] = []
        for record in records:
            if record.status is not InstallationState.ACTIVE:
                refreshed.append(record)
                continue
            try:
                await self._broker.verify_installation(record.installation_id)
            except InstallationRevokedError:
                await self.revoke_installation(tenant_id, record.installation_id)
            except InstallationSuspendedError:
                await self._repository.set_installation_status(
                    tenant_id, record.installation_id, InstallationState.SUSPENDED
                )
            except CheckoutError:
                # A transport or API failure is not evidence of revocation;
                # keep the persisted state and let the next call retry.
                logger.warning(
                    "live verification failed for installation %d",
                    record.installation_id,
                )
            else:
                await self._repository.set_installation_status(
                    tenant_id,
                    record.installation_id,
                    InstallationState.ACTIVE,
                    verified_at=utc_now(),
                )
            current = await self._repository.get_installation(
                tenant_id, record.installation_id
            )
            if current is not None:
                refreshed.append(current)
        return refreshed

    async def list_repositories(
        self, tenant_id: str, installation_id: int
    ) -> list[GitHubRepositoryRecord]:
        """Return the persisted repository grants for one installation."""
        record = await self._repository.get_installation(tenant_id, installation_id)
        if record is None:
            return []
        return await self._repository.list_repositories(tenant_id, record.id)

    async def revoke_installation(self, tenant_id: str, installation_id: int) -> None:
        """Cascade a revocation: installation REVOKED, grants REMOVED, caches dropped.

        Shared by the webhook ``installation deleted`` path and every
        verify-at-use 404. Idempotent: an unknown installation only drops
        caches.
        """
        record = await self._repository.get_installation(tenant_id, installation_id)
        if record is not None:
            await self._repository.set_installation_status(
                tenant_id, installation_id, InstallationState.REVOKED
            )
            await self._repository.set_repository_status(
                tenant_id, installation_pk=record.id, status=RepositoryState.REMOVED
            )
        self.drop_installation_caches(installation_id)

    def drop_installation_caches(self, installation_id: int) -> None:
        """Drop the broker's token cache and verify memo for one installation.

        The broker (an integrations-layer component) exposes no cache-eviction
        surface of its own; this is the service-side revocation hook it needs,
        reaching into the private maps as a documented wiring seam. A stale
        token would fail closed on use anyway -- this only stops a revoked
        installation's tokens from being offered at all.
        """
        broker_cache = self._broker._cache  # noqa: SLF001 - revocation wiring seam
        for key in [key for key in broker_cache if key[0] == installation_id]:
            broker_cache.pop(key, None)
        self._broker._verified.pop(installation_id, None)  # noqa: SLF001 - same seam

    # -- live synchronization --------------------------------------------------

    async def _sync_installation(
        self, tenant_id: str, installation_id: int
    ) -> GitHubInstallationRecord:
        """Verify one installation live and reconcile the persisted projection."""
        try:
            data = await self._client.get_installation(installation_id)
        except InstallationRevokedError:
            await self.revoke_installation(tenant_id, installation_id)
            raise
        account = data.get("account") or {}
        suspended = bool(data.get("suspended_at"))
        record = await self._repository.upsert_installation(
            tenant_id,
            installation_id=installation_id,
            account_login=str(account.get("login") or ""),
            account_type=str(account.get("type") or ""),
            repository_selection=str(data.get("repository_selection") or "all"),
            status=(
                InstallationState.SUSPENDED if suspended else InstallationState.ACTIVE
            ),
            last_verified_at=utc_now(),
        )
        if suspended:
            # GitHub refuses token mints for a suspended installation, so the
            # grant projection cannot be refreshed until it is unsuspended.
            return record
        await self._refresh_repositories(tenant_id, record)
        refreshed = await self._repository.get_installation(tenant_id, installation_id)
        return refreshed if refreshed is not None else record

    async def _refresh_repositories(
        self, tenant_id: str, record: GitHubInstallationRecord
    ) -> None:
        """Reconcile persisted grants against GitHub's live repository listing."""
        token = await self._mint_installation_wide_token(record.installation_id)
        try:
            grants = await self._client.list_installation_repositories(token)
        finally:
            await self._client.revoke_installation_token(token)
        for grant in grants:
            await self._repository.upsert_repository(
                tenant_id, installation_pk=record.id, grant=grant
            )
        live_repo_ids = {grant.repo_id for grant in grants}
        for row in await self._repository.list_repositories(tenant_id, record.id):
            if row.repo_id not in live_repo_ids and row.status is RepositoryState.ACTIVE:
                await self._repository.set_repository_status(
                    tenant_id,
                    installation_pk=record.id,
                    repo_id=row.repo_id,
                    status=RepositoryState.REMOVED,
                )

    async def _mint_installation_wide_token(self, installation_id: int) -> str:
        """Mint an installation token without a repository restriction.

        :meth:`GitHubAppClient.mint_installation_token` always restricts the
        token to one named repository, which makes it unable to enumerate an
        installation's grants -- a restricted token lists only its own scope.
        This performs the same App-JWT-authenticated mint with no
        ``repositories`` body through the same governed client (same purpose,
        base URL, timeout, and transport, so the pooled client is shared) and
        maps failures onto the same typed vocabulary. The token is returned to
        the caller for one listing and never cached, logged, or persisted.
        """
        operation = "installation token mint"
        app_jwt = await self._jwt_issuer.issue()
        client = await governed_async_client(
            purpose="github-app",
            timeout=self._timeout_seconds,
            base_url=self._config.api_base_url,
            transport=self._transport,
        )
        try:
            response = await client.request(
                "POST",
                f"/app/installations/{installation_id}/access_tokens",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {app_jwt}",
                },
            )
        except httpx.HTTPError as exc:
            raise CheckoutError(
                CheckoutFailureCode.API_ERROR,
                f"github api request failed during {operation}",
            ) from exc
        if response.status_code == 404:
            raise InstallationRevokedError()
        if response.status_code == 403:
            if self._mentions_suspension(response):
                raise InstallationSuspendedError()
            raise GitHubApiError(response.status_code, operation)
        if response.status_code != 201:
            raise GitHubApiError(response.status_code, operation)
        payload = response.json()
        return str(payload["token"])

    @staticmethod
    def _mentions_suspension(response: httpx.Response) -> bool:
        """Classify a 403 body without letting its text escape into errors."""
        try:
            message: Any = response.json().get("message", "")
        except ValueError:
            return False
        return isinstance(message, str) and "suspend" in message.lower()


__all__ = ["GitHubIntegrationService"]
