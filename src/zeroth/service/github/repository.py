"""Async database-backed persistence for the GitHub App integration surface.

Follows the deployments recipe: per-call tenant scoping through
:class:`~zeroth.platform.storage.ScopedTable`, one
``@persistence_surface`` declaration per registered resource, and explicit
``@persistence_operation`` metadata on every public method. Installations are
tenant-level resources, so every table binds a
:class:`NullWorkspaceScopeContext`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from zeroth.integrations.github.models import (
    InstallationState,
    RepositoryGrant,
    RepositoryState,
)
from zeroth.platform.primitives import utc_now
from zeroth.platform.storage import (
    SERVICE_SCOPE_REGISTRY,
    AsyncDatabase,
    NullWorkspaceScopeContext,
    ResourceOperation,
    ScopedTable,
)
from zeroth.platform.storage.scoping import (
    named_isolation_probe,
    persistence_operation,
    persistence_surface,
)


def _iso(value: datetime | None) -> str | None:
    """Serialize an optional datetime to the stored ISO-8601 TEXT form."""
    return value.isoformat() if value is not None else None


def _parse(value: object) -> datetime | None:
    """Parse an optional stored ISO-8601 TEXT column back to a datetime."""
    return datetime.fromisoformat(value) if isinstance(value, str) and value else None


@dataclass(frozen=True, slots=True)
class GitHubInstallationRecord:
    """One persisted GitHub App installation, as the platform tracks it."""

    id: str
    tenant_id: str
    installation_id: int
    account_login: str
    account_type: str
    repository_selection: str
    status: InstallationState
    last_verified_at: datetime | None
    suspended_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class GitHubRepositoryRecord:
    """One repository grant persisted under a tracked installation."""

    id: str
    tenant_id: str
    installation_pk: str
    repo_id: int
    owner: str
    name: str
    full_name: str
    private: bool
    default_branch: str
    status: RepositoryState
    added_at: datetime
    removed_at: datetime | None


@persistence_surface(
    "service.github_installations",
    probe=named_isolation_probe("_drive_github_installations"),
    method_names=frozenset(
        {
            "upsert_installation",
            "get_installation",
            "list_installations",
            "set_installation_status",
        }
    ),
)
@persistence_surface(
    "service.github_repositories",
    probe=named_isolation_probe("_drive_github_repositories"),
    method_names=frozenset(
        {
            "upsert_repository",
            "get_repository",
            "list_repositories",
            "set_repository_status",
        }
    ),
)
@persistence_surface(
    "service.github_webhook_deliveries",
    probe=named_isolation_probe("_drive_github_webhook_deliveries"),
    method_names=frozenset({"record_delivery", "prune_deliveries"}),
)
class SQLiteGitHubRepository:
    """Persist installations, repository grants, and webhook delivery dedup."""

    def __init__(self, database: AsyncDatabase):
        self._database: AsyncDatabase = database

    def _scope(self, tenant_id: str | None) -> NullWorkspaceScopeContext:
        """Build the tenant scope for one call, honoring the default-compat tenant."""
        tenant = tenant_id or "default"
        if tenant == "default":
            return NullWorkspaceScopeContext.for_default_compatibility()
        return NullWorkspaceScopeContext(tenant_id=tenant)

    def _installations(self, tenant_id: str | None) -> ScopedTable:
        """Bind the installations table to one tenant scope."""
        return ScopedTable(
            self._database,
            SERVICE_SCOPE_REGISTRY,
            "service.github_installations",
            self._scope(tenant_id),
        )

    def _repositories(self, tenant_id: str | None) -> ScopedTable:
        """Bind the repository-grants table to one tenant scope."""
        return ScopedTable(
            self._database,
            SERVICE_SCOPE_REGISTRY,
            "service.github_repositories",
            self._scope(tenant_id),
        )

    def _deliveries(self, tenant_id: str | None) -> ScopedTable:
        """Bind the webhook-delivery dedup table to one tenant scope."""
        return ScopedTable(
            self._database,
            SERVICE_SCOPE_REGISTRY,
            "service.github_webhook_deliveries",
            self._scope(tenant_id),
        )

    # -- installations ---------------------------------------------------------

    @persistence_operation(
        ResourceOperation.CREATE, ResourceOperation.READ, ResourceOperation.UPDATE
    )
    async def upsert_installation(
        self,
        tenant_id: str | None,
        *,
        installation_id: int,
        account_login: str,
        account_type: str,
        repository_selection: str,
        status: InstallationState | None = None,
        last_verified_at: datetime | None = None,
    ) -> GitHubInstallationRecord:
        """Insert or refresh one installation row and return its current state.

        A new row is created in ``status`` (default ``PENDING_CLAIM``). An
        existing row keeps its status unless ``status`` is passed explicitly,
        so a webhook re-delivery cannot silently reset a lifecycle transition.
        """
        now = utc_now()
        stamp = now.isoformat()
        async with self._installations(tenant_id).transaction(write_lock=True) as installations:
            row = await installations.select_one(where={"installation_id": installation_id})
            if row is None:
                created_status = status or InstallationState.PENDING_CLAIM
                inserted = await installations.insert_if_absent(
                    {
                        "id": uuid4().hex,
                        "installation_id": installation_id,
                        "account_login": account_login,
                        "account_type": account_type,
                        "repository_selection": repository_selection,
                        "status": created_status.value,
                        "last_verified_at": _iso(last_verified_at),
                        "suspended_at": (
                            stamp if created_status is InstallationState.SUSPENDED else None
                        ),
                        "revoked_at": (
                            stamp if created_status is InstallationState.REVOKED else None
                        ),
                        "created_at": stamp,
                        "updated_at": stamp,
                    },
                    conflict_columns=("tenant_id", "installation_id"),
                )
                if not inserted:
                    row = await installations.select_one(
                        where={"installation_id": installation_id}
                    )
            if row is not None:
                values: dict[str, object] = {
                    "account_login": account_login,
                    "account_type": account_type,
                    "repository_selection": repository_selection,
                    "updated_at": stamp,
                }
                if status is not None:
                    values["status"] = status.value
                    if status is InstallationState.SUSPENDED:
                        values["suspended_at"] = stamp
                    elif status is InstallationState.REVOKED:
                        values["revoked_at"] = stamp
                    elif status is InstallationState.ACTIVE:
                        values["suspended_at"] = None
                if last_verified_at is not None:
                    values["last_verified_at"] = last_verified_at.isoformat()
                await installations.update(
                    values, where={"installation_id": installation_id}
                )
            final = await installations.select_one(where={"installation_id": installation_id})
        if final is None:  # pragma: no cover - the row was written above
            raise KeyError(installation_id)
        return self._row_to_installation(final)

    @persistence_operation(ResourceOperation.READ)
    async def get_installation(
        self, tenant_id: str | None, installation_id: int
    ) -> GitHubInstallationRecord | None:
        """Load one installation by its GitHub installation id, or ``None``."""
        row = await self._installations(tenant_id).select_one(
            where={"installation_id": installation_id}
        )
        return None if row is None else self._row_to_installation(row)

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def list_installations(
        self, tenant_id: str | None
    ) -> list[GitHubInstallationRecord]:
        """Return every tracked installation for one tenant, oldest first."""
        async with self._installations(tenant_id).transaction() as installations:
            rows = await installations.select(order_by=("created_at", "installation_id"))
        return [self._row_to_installation(row) for row in rows]

    @persistence_operation(ResourceOperation.UPDATE)
    async def set_installation_status(
        self,
        tenant_id: str | None,
        installation_id: int,
        status: InstallationState,
        *,
        verified_at: datetime | None = None,
    ) -> None:
        """Transition one installation's lifecycle status (no-op when absent)."""
        stamp = utc_now().isoformat()
        values: dict[str, object] = {"status": status.value, "updated_at": stamp}
        if status is InstallationState.SUSPENDED:
            values["suspended_at"] = stamp
        elif status is InstallationState.REVOKED:
            values["revoked_at"] = stamp
        elif status is InstallationState.ACTIVE:
            values["suspended_at"] = None
        if verified_at is not None:
            values["last_verified_at"] = verified_at.isoformat()
        await self._installations(tenant_id).update(
            values, where={"installation_id": installation_id}
        )

    # -- repository grants -----------------------------------------------------

    @persistence_operation(
        ResourceOperation.CREATE, ResourceOperation.READ, ResourceOperation.UPDATE
    )
    async def upsert_repository(
        self,
        tenant_id: str | None,
        *,
        installation_pk: str,
        grant: RepositoryGrant,
    ) -> GitHubRepositoryRecord:
        """Insert or refresh one repository grant under an installation row.

        Re-adding a previously removed repository reactivates the same row
        (status back to the grant's state, ``removed_at`` cleared).
        """
        stamp = utc_now().isoformat()
        identity = {"installation_pk": installation_pk, "repo_id": grant.repo_id}
        async with self._repositories(tenant_id).transaction(write_lock=True) as repositories:
            row = await repositories.select_one(where=identity)
            if row is None:
                inserted = await repositories.insert_if_absent(
                    {
                        "id": uuid4().hex,
                        "installation_pk": installation_pk,
                        "repo_id": grant.repo_id,
                        "owner": grant.owner,
                        "name": grant.name,
                        "full_name": grant.full_name,
                        "private": 1 if grant.private else 0,
                        "default_branch": grant.default_branch,
                        "status": grant.state.value,
                        "added_at": stamp,
                        "removed_at": None,
                    },
                    conflict_columns=("tenant_id", "installation_pk", "repo_id"),
                )
                if not inserted:
                    row = await repositories.select_one(where=identity)
            if row is not None:
                await repositories.update(
                    {
                        "owner": grant.owner,
                        "name": grant.name,
                        "full_name": grant.full_name,
                        "private": 1 if grant.private else 0,
                        "default_branch": grant.default_branch,
                        "status": grant.state.value,
                        "removed_at": (
                            stamp if grant.state is RepositoryState.REMOVED else None
                        ),
                    },
                    where=identity,
                )
            final = await repositories.select_one(where=identity)
        if final is None:  # pragma: no cover - the row was written above
            raise KeyError(grant.repo_id)
        return self._row_to_repository(final)

    @persistence_operation(ResourceOperation.READ)
    async def get_repository(
        self, tenant_id: str | None, installation_pk: str, repo_id: int
    ) -> GitHubRepositoryRecord | None:
        """Load one repository grant by installation row and repo id, or ``None``."""
        row = await self._repositories(tenant_id).select_one(
            where={"installation_pk": installation_pk, "repo_id": repo_id}
        )
        return None if row is None else self._row_to_repository(row)

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def list_repositories(
        self, tenant_id: str | None, installation_pk: str
    ) -> list[GitHubRepositoryRecord]:
        """Return every grant row under one installation, stable by full name."""
        async with self._repositories(tenant_id).transaction() as repositories:
            rows = await repositories.select(
                where={"installation_pk": installation_pk},
                order_by=("full_name", "repo_id"),
            )
        return [self._row_to_repository(row) for row in rows]

    @persistence_operation(ResourceOperation.UPDATE)
    async def set_repository_status(
        self,
        tenant_id: str | None,
        *,
        installation_pk: str,
        status: RepositoryState,
        repo_id: int | None = None,
    ) -> None:
        """Transition one grant (or every grant under an installation) to ``status``."""
        stamp = utc_now().isoformat()
        values: dict[str, object] = {
            "status": status.value,
            "removed_at": stamp if status is RepositoryState.REMOVED else None,
        }
        where: dict[str, object] = {"installation_pk": installation_pk}
        if repo_id is not None:
            where["repo_id"] = repo_id
        await self._repositories(tenant_id).update(values, where=where)

    # -- webhook delivery dedup ------------------------------------------------

    @persistence_operation(ResourceOperation.CREATE)
    async def record_delivery(
        self,
        tenant_id: str | None,
        delivery_guid: str,
        *,
        event: str,
        action: str | None,
        installation_id: int | None,
    ) -> bool:
        """Record one webhook delivery GUID; ``False`` marks a duplicate."""
        return await self._deliveries(tenant_id).insert_if_absent(
            {
                "delivery_guid": delivery_guid,
                "event": event,
                "action": action,
                "installation_id": installation_id,
                "received_at": utc_now().isoformat(),
                "handled": 1,
            },
            conflict_columns=("tenant_id", "delivery_guid"),
        )

    @persistence_operation(ResourceOperation.ENUMERATE, ResourceOperation.DELETE)
    async def prune_deliveries(
        self, tenant_id: str | None, older_than: datetime
    ) -> int:
        """Delete delivery rows received before ``older_than``; return the count."""
        cutoff = older_than.isoformat()
        async with self._deliveries(tenant_id).transaction(write_lock=True) as deliveries:
            rows = await deliveries.select(
                columns=("delivery_guid",), where_lt={"received_at": cutoff}
            )
            for row in rows:
                await deliveries.delete(where={"delivery_guid": row["delivery_guid"]})
        return len(rows)

    # -- row hydration ---------------------------------------------------------

    def _row_to_installation(self, row: dict[str, object]) -> GitHubInstallationRecord:
        """Convert a database row to a :class:`GitHubInstallationRecord`."""
        created_at = _parse(row["created_at"])
        updated_at = _parse(row["updated_at"])
        if created_at is None or updated_at is None:  # pragma: no cover - NOT NULL
            raise ValueError("installation row is missing its timestamps")
        return GitHubInstallationRecord(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            installation_id=int(row["installation_id"]),  # type: ignore[arg-type]
            account_login=str(row["account_login"]),
            account_type=str(row["account_type"]),
            repository_selection=str(row["repository_selection"]),
            status=InstallationState(str(row["status"])),
            last_verified_at=_parse(row["last_verified_at"]),
            suspended_at=_parse(row["suspended_at"]),
            revoked_at=_parse(row["revoked_at"]),
            created_at=created_at,
            updated_at=updated_at,
        )

    def _row_to_repository(self, row: dict[str, object]) -> GitHubRepositoryRecord:
        """Convert a database row to a :class:`GitHubRepositoryRecord`."""
        added_at = _parse(row["added_at"])
        if added_at is None:  # pragma: no cover - NOT NULL column
            raise ValueError("repository row is missing its added_at timestamp")
        return GitHubRepositoryRecord(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            installation_pk=str(row["installation_pk"]),
            repo_id=int(row["repo_id"]),  # type: ignore[arg-type]
            owner=str(row["owner"]),
            name=str(row["name"]),
            full_name=str(row["full_name"]),
            private=bool(row["private"]),
            default_branch=str(row["default_branch"]),
            status=RepositoryState(str(row["status"])),
            added_at=added_at,
            removed_at=_parse(row["removed_at"]),
        )


__all__ = [
    "GitHubInstallationRecord",
    "GitHubRepositoryRecord",
    "SQLiteGitHubRepository",
]
