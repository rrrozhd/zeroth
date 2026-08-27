"""GitHub integration persistence: round-trips, tenant isolation, and structure."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from zeroth.integrations.github.models import (
    InstallationState,
    RepositoryGrant,
    RepositoryState,
)
from zeroth.platform.primitives import utc_now
from zeroth.platform.storage import (
    SERVICE_SCOPE_REGISTRY,
    ResourceOperation,
    validate_persistence_surface,
)
from zeroth.platform.storage.async_sqlite import AsyncSQLiteDatabase
from zeroth.platform.storage.scoped_table import ASYNC_PERSISTENCE_MODULES
from zeroth.platform.storage.service_surfaces import (
    executable_probe_for,
    load_service_persistence_surfaces,
)
from zeroth.service.bootstrap.migrations import run_migrations
from zeroth.service.github.repository import SQLiteGitHubRepository

GITHUB_RESOURCES = (
    "service.github_installations",
    "service.github_repositories",
    "service.github_webhook_deliveries",
)


def _grant(repo_id: int = 9001, full_name: str = "acme/repo-one") -> RepositoryGrant:
    owner, _, name = full_name.partition("/")
    return RepositoryGrant(
        repo_id=repo_id,
        owner=owner,
        name=name,
        full_name=full_name,
        private=True,
        default_branch="main",
    )


async def _seed_installation(repo: SQLiteGitHubRepository, tenant: str, installation_id=501):
    return await repo.upsert_installation(
        tenant,
        installation_id=installation_id,
        account_login="acme",
        account_type="Organization",
        repository_selection="selected",
    )


# -- recipe round-trips --------------------------------------------------------


async def test_installation_upsert_get_list_round_trip(sqlite_db) -> None:
    repo = SQLiteGitHubRepository(sqlite_db)
    created = await _seed_installation(repo, "tenant-a")
    assert created.status is InstallationState.PENDING_CLAIM
    assert created.account_login == "acme"
    assert created.created_at is not None

    # A re-delivery updates the account projection without resetting status.
    await repo.set_installation_status("tenant-a", 501, InstallationState.ACTIVE)
    updated = await repo.upsert_installation(
        "tenant-a",
        installation_id=501,
        account_login="acme-renamed",
        account_type="Organization",
        repository_selection="all",
    )
    assert updated.account_login == "acme-renamed"
    assert updated.repository_selection == "all"
    assert updated.status is InstallationState.ACTIVE

    fetched = await repo.get_installation("tenant-a", 501)
    assert fetched is not None and fetched.id == created.id
    assert [row.installation_id for row in await repo.list_installations("tenant-a")] == [501]
    assert await repo.get_installation("tenant-a", 999) is None


async def test_installation_status_transitions_stamp_timestamps(sqlite_db) -> None:
    repo = SQLiteGitHubRepository(sqlite_db)
    await _seed_installation(repo, "tenant-a")

    await repo.set_installation_status("tenant-a", 501, InstallationState.SUSPENDED)
    suspended = await repo.get_installation("tenant-a", 501)
    assert suspended is not None
    assert suspended.status is InstallationState.SUSPENDED
    assert suspended.suspended_at is not None

    verified = utc_now()
    await repo.set_installation_status(
        "tenant-a", 501, InstallationState.ACTIVE, verified_at=verified
    )
    active = await repo.get_installation("tenant-a", 501)
    assert active is not None
    assert active.status is InstallationState.ACTIVE
    assert active.suspended_at is None
    assert active.last_verified_at == verified

    await repo.set_installation_status("tenant-a", 501, InstallationState.REVOKED)
    revoked = await repo.get_installation("tenant-a", 501)
    assert revoked is not None
    assert revoked.status is InstallationState.REVOKED
    assert revoked.revoked_at is not None


async def test_repository_upsert_list_and_status_round_trip(sqlite_db) -> None:
    repo = SQLiteGitHubRepository(sqlite_db)
    installation = await _seed_installation(repo, "tenant-a")

    first = await repo.upsert_repository(
        "tenant-a", installation_pk=installation.id, grant=_grant()
    )
    assert first.full_name == "acme/repo-one"
    assert first.private is True
    assert first.status is RepositoryState.ACTIVE

    await repo.upsert_repository(
        "tenant-a", installation_pk=installation.id, grant=_grant(9002, "acme/repo-two")
    )
    names = [row.full_name for row in await repo.list_repositories("tenant-a", installation.id)]
    assert names == ["acme/repo-one", "acme/repo-two"]

    await repo.set_repository_status(
        "tenant-a",
        installation_pk=installation.id,
        repo_id=9001,
        status=RepositoryState.REMOVED,
    )
    removed = await repo.get_repository("tenant-a", installation.id, 9001)
    assert removed is not None
    assert removed.status is RepositoryState.REMOVED
    assert removed.removed_at is not None

    # Re-adding the same repo reactivates the same row.
    revived = await repo.upsert_repository(
        "tenant-a", installation_pk=installation.id, grant=_grant()
    )
    assert revived.id == first.id
    assert revived.status is RepositoryState.ACTIVE
    assert revived.removed_at is None

    # The whole-installation cascade removes every grant at once.
    await repo.set_repository_status(
        "tenant-a", installation_pk=installation.id, status=RepositoryState.REMOVED
    )
    statuses = {
        row.status for row in await repo.list_repositories("tenant-a", installation.id)
    }
    assert statuses == {RepositoryState.REMOVED}


async def test_delivery_dedup_and_prune(sqlite_db) -> None:
    repo = SQLiteGitHubRepository(sqlite_db)
    assert await repo.record_delivery(
        "tenant-a", "guid-1", event="installation", action="created", installation_id=501
    )
    assert not await repo.record_delivery(
        "tenant-a", "guid-1", event="installation", action="created", installation_id=501
    )
    assert await repo.record_delivery(
        "tenant-a", "guid-2", event="push", action=None, installation_id=None
    )

    # Nothing is old enough yet; then everything is.
    assert await repo.prune_deliveries("tenant-a", utc_now() - timedelta(days=7)) == 0
    assert await repo.prune_deliveries("tenant-a", utc_now() + timedelta(seconds=1)) == 2
    assert await repo.record_delivery(
        "tenant-a", "guid-1", event="installation", action="created", installation_id=501
    )


# -- cross-tenant isolation (restart-shaped, per tests/security pattern) -------


async def test_reopened_database_hides_github_rows_from_other_tenants(tmp_path: Path) -> None:
    database_path = tmp_path / "github-restart.db"
    run_migrations(f"sqlite:///{database_path}")
    first = AsyncSQLiteDatabase(str(database_path))
    try:
        repo = SQLiteGitHubRepository(first)
        installation = await _seed_installation(repo, "tenant-a")
        await repo.upsert_repository(
            "tenant-a", installation_pk=installation.id, grant=_grant()
        )
        await repo.record_delivery(
            "tenant-a", "guid-1", event="installation", action="created", installation_id=501
        )
    finally:
        await first.close()

    restarted = AsyncSQLiteDatabase(str(database_path))
    try:
        repo = SQLiteGitHubRepository(restarted)
        assert await repo.get_installation("tenant-b", 501) is None
        assert await repo.get_installation("tenant-b", 999) is None
        assert await repo.list_installations("tenant-b") == []
        assert await repo.get_repository("tenant-b", installation.id, 9001) is None
        assert await repo.list_repositories("tenant-b", installation.id) == []
        # The owner's GUID does not collide across the tenant boundary...
        assert await repo.record_delivery(
            "tenant-b", "guid-1", event="installation", action="created", installation_id=501
        )
        # ...and a foreign prune sweeps nothing of the owner's.
        await repo.prune_deliveries("tenant-b", utc_now() + timedelta(days=1))
        assert not await repo.record_delivery(
            "tenant-a", "guid-1", event="installation", action="created", installation_id=501
        )
        owner = await repo.get_installation("tenant-a", 501)
        assert owner is not None and owner.account_login == "acme"
    finally:
        await restarted.close()


# -- structural surface validation ---------------------------------------------


def test_github_surfaces_are_discovered_and_structurally_valid() -> None:
    surfaces = {
        surface.resource_name: surface
        for surface in load_service_persistence_surfaces()
        if surface.resource_name in GITHUB_RESOURCES
    }
    assert set(surfaces) == set(GITHUB_RESOURCES)
    for resource_name, surface in surfaces.items():
        definition = SERVICE_SCOPE_REGISTRY.definition_for_resource(resource_name)
        validate_persistence_surface(surface, definition)
        declared = frozenset(
            operation
            for operations in surface.operation_methods.values()
            for operation in operations
        )
        assert declared == definition.operations


def test_github_repository_module_is_classified_as_persistence() -> None:
    assert "service/github/repository.py" in ASYNC_PERSISTENCE_MODULES


async def test_every_github_registry_operation_has_an_executable_probe(tmp_path: Path) -> None:
    """The security matrix picks the new resources up: every case has a probe."""
    surfaces = load_service_persistence_surfaces()
    for resource_name in GITHUB_RESOURCES:
        definition = SERVICE_SCOPE_REGISTRY.definition_for_resource(resource_name)
        for operation in sorted(definition.operations, key=lambda item: item.value):
            probe = executable_probe_for(surfaces, resource_name, operation)
            database_path = tmp_path / f"{definition.table_name}-{operation.value}.db"
            run_migrations(f"sqlite:///{database_path}")
            database = AsyncSQLiteDatabase(str(database_path))
            try:
                await probe(database, operation=operation)
            finally:
                await database.close()


def test_github_registry_operations_are_the_reviewed_sets() -> None:
    operations = {
        name: SERVICE_SCOPE_REGISTRY.definition_for_resource(name).operations
        for name in GITHUB_RESOURCES
    }
    ops = ResourceOperation
    assert operations["service.github_installations"] == {
        ops.CREATE, ops.READ, ops.ENUMERATE, ops.UPDATE
    }
    assert operations["service.github_repositories"] == {
        ops.CREATE, ops.READ, ops.ENUMERATE, ops.UPDATE
    }
    assert operations["service.github_webhook_deliveries"] == {
        ops.CREATE, ops.ENUMERATE, ops.DELETE
    }
