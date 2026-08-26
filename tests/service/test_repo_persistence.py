"""Repo checkout/run persistence: round-trips, leases, isolation, and structure."""

from __future__ import annotations

import inspect
from datetime import timedelta
from pathlib import Path

import pytest

from zeroth.integrations.github.checkout import CheckoutStateStore
from zeroth.integrations.github.models import CheckoutFailureCode, RepositoryGrant
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
from zeroth.service.repositories.repo_models import (
    INPUT_PAYLOAD_CAP_BYTES,
    OUTPUT_PAYLOAD_CAP_BYTES,
    RepoCheckout,
    RepoCheckoutState,
    RepoRun,
    RepoRunState,
)
from zeroth.service.repositories.repository import (
    SQLiteRepoCheckoutRepository,
    SQLiteRepoRunRepository,
)

REPO_RESOURCES = ("service.repo_checkouts", "service.repo_runs")


async def _seed_parent(database, tenant: str, *, installation_id: int = 501) -> str:
    """Seed the GitHub installation+grant chain and return the grant row id."""
    github = SQLiteGitHubRepository(database)
    installation = await github.upsert_installation(
        tenant,
        installation_id=installation_id,
        account_login="acme",
        account_type="Organization",
        repository_selection="selected",
    )
    record = await github.upsert_repository(
        tenant,
        installation_pk=installation.id,
        grant=RepositoryGrant(
            repo_id=9001,
            owner="acme",
            name="repo-one",
            full_name="acme/repo-one",
            private=True,
            default_branch="main",
        ),
    )
    return record.id


def _checkout(tenant: str, repository_pk: str, **overrides) -> RepoCheckout:
    defaults = dict(
        tenant_id=tenant,
        repository_pk=repository_pk,
        installation_id=501,
        repository_id=9001,
        repository_full_name="acme/repo-one",
        requested_ref="main",
    )
    defaults.update(overrides)
    return RepoCheckout(**defaults)


async def _staged_checkout(database, tenant: str = "tenant-a") -> RepoCheckout:
    """Create a checkout row already transitioned to STAGED."""
    pk = await _seed_parent(database, tenant)
    checkouts = SQLiteRepoCheckoutRepository(database)
    created = await checkouts.create(_checkout(tenant, pk))
    await checkouts.transition_state(tenant, created.id, RepoCheckoutState.STAGED)
    staged = await checkouts.get(tenant, created.id)
    assert staged is not None
    return staged


# -- checkout round-trips ------------------------------------------------------


async def test_checkout_create_get_list_round_trip(sqlite_db) -> None:
    pk = await _seed_parent(sqlite_db, "tenant-a")
    repo = SQLiteRepoCheckoutRepository(sqlite_db)
    created = await repo.create(
        _checkout(
            "tenant-a",
            pk,
            requested_ref="refs/heads/main",
            script_name="ingest",
            config_digest="sha256:config",
            manifest_digest="sha256:manifest",
        )
    )
    assert created.state is RepoCheckoutState.REQUESTED
    assert created.cache_hit is False
    assert created.has_lfs_pointers is None

    fetched = await repo.get("tenant-a", created.id)
    assert fetched is not None
    assert fetched.repository_pk == pk
    assert fetched.requested_ref == "refs/heads/main"
    assert fetched.script_name == "ingest"
    assert fetched.config_digest == "sha256:config"
    assert fetched.manifest_digest == "sha256:manifest"
    assert fetched.failure_code is None

    assert [row.id for row in await repo.list_checkouts("tenant-a")] == [created.id]
    assert await repo.get("tenant-a", "unknown-checkout") is None
    assert (
        await repo.list_checkouts("tenant-a", state=RepoCheckoutState.FAILED) == []
    )


async def test_checkout_walks_every_forward_lifecycle_state(sqlite_db) -> None:
    pk = await _seed_parent(sqlite_db, "tenant-a")
    repo = SQLiteRepoCheckoutRepository(sqlite_db)
    created = await repo.create(_checkout("tenant-a", pk))

    for state in (
        RepoCheckoutState.VERIFYING,
        RepoCheckoutState.FETCHING,
        RepoCheckoutState.HARDENING,
    ):
        assert await repo.transition_state("tenant-a", created.id, state)
        current = await repo.get("tenant-a", created.id)
        assert current is not None and current.state is state

    verified = utc_now()
    assert await repo.transition_state(
        "tenant-a",
        created.id,
        RepoCheckoutState.STAGED,
        resolved_commit_sha="a" * 40,
        git_tree_id="b" * 40,
        tree_digest="sha256:tree",
        staged_path="/staged/checkout",
        file_count=12,
        size_bytes=4096,
        has_lfs_pointers=False,
        cache_hit=True,
        verified_at=verified,
        expires_at=verified + timedelta(hours=1),
    )
    staged = await repo.get("tenant-a", created.id)
    assert staged is not None
    assert staged.state is RepoCheckoutState.STAGED
    assert staged.resolved_commit_sha == "a" * 40
    assert staged.git_tree_id == "b" * 40
    assert staged.tree_digest == "sha256:tree"
    assert staged.staged_path == "/staged/checkout"
    assert staged.file_count == 12
    assert staged.size_bytes == 4096
    assert staged.has_lfs_pointers is False
    assert staged.cache_hit is True
    assert staged.verified_at == verified
    assert staged.updated_at >= created.updated_at

    # Consuming does not null identities recorded by the staging transition.
    assert await repo.transition_state("tenant-a", created.id, RepoCheckoutState.CONSUMED)
    consumed = await repo.get("tenant-a", created.id)
    assert consumed is not None
    assert consumed.state is RepoCheckoutState.CONSUMED
    assert consumed.resolved_commit_sha == "a" * 40
    assert consumed.tree_digest == "sha256:tree"


async def test_record_failure_sets_state_code_and_redacted_detail(sqlite_db) -> None:
    pk = await _seed_parent(sqlite_db, "tenant-a")
    repo = SQLiteRepoCheckoutRepository(sqlite_db)
    created = await repo.create(_checkout("tenant-a", pk))

    assert await repo.record_failure(
        "tenant-a",
        created.id,
        code=CheckoutFailureCode.COMMIT_UNREACHABLE,
        redacted_detail="pinned SHA is unreachable",
    )
    failed = await repo.get("tenant-a", created.id)
    assert failed is not None
    assert failed.state is RepoCheckoutState.FAILED
    assert failed.failure_code is CheckoutFailureCode.COMMIT_UNREACHABLE
    assert failed.failure_detail == "pinned SHA is unreachable"
    assert not await repo.record_failure(
        "tenant-a",
        "unknown-checkout",
        code=CheckoutFailureCode.GIT_ERROR,
        redacted_detail="detail",
    )


async def test_expiry_sweep_expires_only_staged_rows_past_their_horizon(sqlite_db) -> None:
    pk = await _seed_parent(sqlite_db, "tenant-a")
    repo = SQLiteRepoCheckoutRepository(sqlite_db)
    now = utc_now()

    expired = await repo.create(_checkout("tenant-a", pk))
    await repo.transition_state(
        "tenant-a", expired.id, RepoCheckoutState.STAGED, expires_at=now - timedelta(minutes=1)
    )
    fresh = await repo.create(_checkout("tenant-a", pk))
    await repo.transition_state(
        "tenant-a", fresh.id, RepoCheckoutState.STAGED, expires_at=now + timedelta(hours=1)
    )
    requested = await repo.create(_checkout("tenant-a", pk, expires_at=now - timedelta(hours=1)))

    swept = await repo.expire_stale("tenant-a", now=now)
    assert swept == [expired.id]
    states = {
        row.id: row.state for row in await repo.list_checkouts("tenant-a")
    }
    assert states[expired.id] is RepoCheckoutState.EXPIRED
    assert states[fresh.id] is RepoCheckoutState.STAGED
    assert states[requested.id] is RepoCheckoutState.REQUESTED
    # The sweep is idempotent: nothing left to expire at the same horizon.
    assert await repo.expire_stale("tenant-a", now=now) == []


# -- run round-trips and lease fencing -----------------------------------------


async def test_run_create_get_list_round_trip(sqlite_db) -> None:
    checkout = await _staged_checkout(sqlite_db)
    repo = SQLiteRepoRunRepository(sqlite_db)
    created = await repo.create(
        RepoRun(
            tenant_id="tenant-a",
            checkout_id=checkout.id,
            script_name="ingest",
            input_payload_json='{"limit": 5}',
        )
    )
    assert created.state is RepoRunState.PENDING
    assert created.claim_generation == 0
    assert created.lease_expires_at == created.created_at  # due immediately

    fetched = await repo.get("tenant-a", created.id)
    assert fetched is not None
    assert fetched.checkout_id == checkout.id
    assert fetched.input_payload_json == '{"limit": 5}'
    assert fetched.smoke_passed is None

    assert [row.id for row in await repo.list_runs("tenant-a")] == [created.id]
    assert [
        row.id for row in await repo.list_runs("tenant-a", checkout_id=checkout.id)
    ] == [created.id]
    assert await repo.list_runs("tenant-a", checkout_id="unknown-checkout") == []


async def test_run_claim_is_lease_fenced_two_claimants_one_wins(sqlite_db) -> None:
    checkout = await _staged_checkout(sqlite_db)
    repo = SQLiteRepoRunRepository(sqlite_db)
    run = await repo.create(
        RepoRun(tenant_id="tenant-a", checkout_id=checkout.id, script_name="ingest")
    )

    first = await repo.claim_pending("tenant-a", worker_id="worker-a")
    assert first is not None
    assert first.run.id == run.id
    assert first.generation == 1
    assert first.run.claimed_by == "worker-a"
    assert first.run.state is RepoRunState.RUNNING
    assert first.run.started_at is not None

    # Second claimant loses while the first worker's lease is live.
    assert await repo.claim_pending("tenant-a", worker_id="worker-b") is None


async def test_expired_lease_reclaim_increments_generation_and_fences_the_loser(
    sqlite_db,
) -> None:
    checkout = await _staged_checkout(sqlite_db)
    repo = SQLiteRepoRunRepository(sqlite_db)
    run = await repo.create(
        RepoRun(tenant_id="tenant-a", checkout_id=checkout.id, script_name="ingest")
    )

    first = await repo.claim_pending("tenant-a", worker_id="worker-a", lease_seconds=0.0)
    assert first is not None and first.generation == 1

    # The zero-second lease has lapsed: worker-b reclaims at the next generation.
    second = await repo.claim_pending("tenant-a", worker_id="worker-b")
    assert second is not None
    assert second.generation == 2
    assert second.run.claimed_by == "worker-b"

    # The dead worker's stale generation can neither finish nor fail the run...
    assert not await repo.finish("tenant-a", run.id, first.generation, exit_code=0)
    assert not await repo.fail("tenant-a", run.id, first.generation, failure_code="timeout")
    current = await repo.get("tenant-a", run.id)
    assert current is not None and current.state is RepoRunState.RUNNING
    # ...while the live generation completes it exactly once.
    assert await repo.finish(
        "tenant-a",
        run.id,
        second.generation,
        exit_code=0,
        smoke_passed=True,
        output_payload_json='{"rows": 5}',
    )
    finished = await repo.get("tenant-a", run.id)
    assert finished is not None
    assert finished.state is RepoRunState.SUCCEEDED
    assert finished.exit_code == 0
    assert finished.smoke_passed is True
    assert finished.output_payload_json == '{"rows": 5}'
    assert finished.finished_at is not None
    assert not await repo.finish("tenant-a", run.id, second.generation, exit_code=0)


async def test_run_fail_and_cancel_transitions(sqlite_db) -> None:
    checkout = await _staged_checkout(sqlite_db)
    repo = SQLiteRepoRunRepository(sqlite_db)

    failing = await repo.create(
        RepoRun(tenant_id="tenant-a", checkout_id=checkout.id, script_name="ingest")
    )
    claimed = await repo.claim_pending("tenant-a", worker_id="worker-a")
    assert claimed is not None and claimed.run.id == failing.id
    assert await repo.fail(
        "tenant-a",
        failing.id,
        claimed.generation,
        failure_code="smoke_failed",
        exit_code=3,
        smoke_passed=False,
    )
    failed = await repo.get("tenant-a", failing.id)
    assert failed is not None
    assert failed.state is RepoRunState.FAILED
    assert failed.failure_code == "smoke_failed"
    assert failed.exit_code == 3
    assert failed.smoke_passed is False

    pending = await repo.create(
        RepoRun(tenant_id="tenant-a", checkout_id=checkout.id, script_name="ingest")
    )
    assert await repo.cancel("tenant-a", pending.id)
    cancelled = await repo.get("tenant-a", pending.id)
    assert cancelled is not None and cancelled.state is RepoRunState.CANCELLED
    # A cancelled run is terminal: it can be neither re-cancelled nor claimed.
    assert not await repo.cancel("tenant-a", pending.id)
    assert await repo.claim_pending("tenant-a", worker_id="worker-a") is None


async def test_run_payloads_are_bounded_at_the_model_and_finish_boundaries(sqlite_db) -> None:
    checkout = await _staged_checkout(sqlite_db)
    repo = SQLiteRepoRunRepository(sqlite_db)

    with pytest.raises(ValueError, match="input_payload_json"):
        RepoRun(
            tenant_id="tenant-a",
            checkout_id=checkout.id,
            script_name="ingest",
            input_payload_json="x" * (INPUT_PAYLOAD_CAP_BYTES + 1),
        )
    with pytest.raises(ValueError, match="output_payload_json"):
        RepoRun(
            tenant_id="tenant-a",
            checkout_id=checkout.id,
            script_name="ingest",
            output_payload_json="x" * (OUTPUT_PAYLOAD_CAP_BYTES + 1),
        )

    run = await repo.create(
        RepoRun(tenant_id="tenant-a", checkout_id=checkout.id, script_name="ingest")
    )
    claimed = await repo.claim_pending("tenant-a", worker_id="worker-a")
    assert claimed is not None
    with pytest.raises(ValueError, match="output_payload_json"):
        await repo.finish(
            "tenant-a",
            run.id,
            claimed.generation,
            exit_code=0,
            output_payload_json="x" * (OUTPUT_PAYLOAD_CAP_BYTES + 1),
        )
    # The refused finish left the leased run untouched.
    current = await repo.get("tenant-a", run.id)
    assert current is not None and current.state is RepoRunState.RUNNING


# -- CheckoutStateStore protocol conformance -----------------------------------


def test_checkout_repository_conforms_to_the_checkout_state_store_protocol() -> None:
    protocol_parameters = inspect.signature(CheckoutStateStore.record_state).parameters
    implementation_parameters = inspect.signature(
        SQLiteRepoCheckoutRepository.record_state
    ).parameters
    assert list(implementation_parameters) == list(protocol_parameters)
    assert [item.kind for item in implementation_parameters.values()] == [
        item.kind for item in protocol_parameters.values()
    ]

    # Structural conformance: the repository is assignable where the pipeline
    # expects a CheckoutStateStore.
    store: CheckoutStateStore = SQLiteRepoCheckoutRepository.__new__(
        SQLiteRepoCheckoutRepository
    )
    assert hasattr(store, "record_state")


async def test_record_state_maps_pipeline_states_onto_the_persisted_row(sqlite_db) -> None:
    pk = await _seed_parent(sqlite_db, "tenant-a")
    repo = SQLiteRepoCheckoutRepository(sqlite_db)
    created = await repo.create(_checkout("tenant-a", pk))
    store = repo.for_scope("tenant-a")

    for pipeline_state, expected in (
        ("resolving", RepoCheckoutState.VERIFYING),
        ("fetching", RepoCheckoutState.FETCHING),
        ("scanning", RepoCheckoutState.HARDENING),
        ("materializing", RepoCheckoutState.HARDENING),
        ("verifying", RepoCheckoutState.HARDENING),
    ):
        store.record_state(created.id, pipeline_state)
        await store.flush_state_records()
        current = await repo.get("tenant-a", created.id)
        assert current is not None and current.state is expected

    store.record_state(created.id, "ready", commit_sha="a" * 40, tree_digest="sha256:tree")
    await store.flush_state_records()
    staged = await repo.get("tenant-a", created.id)
    assert staged is not None
    assert staged.state is RepoCheckoutState.STAGED
    assert staged.resolved_commit_sha == "a" * 40
    assert staged.tree_digest == "sha256:tree"

    store.record_state(created.id, "failed", code=CheckoutFailureCode.TREE_DOTGIT.value)
    await store.flush_state_records()
    failed = await repo.get("tenant-a", created.id)
    assert failed is not None
    assert failed.state is RepoCheckoutState.FAILED
    assert failed.failure_code is CheckoutFailureCode.TREE_DOTGIT

    # Unknown pipeline states are ignored rather than corrupting the row.
    store.record_state(created.id, "unmapped-state")
    await store.flush_state_records()
    unchanged = await repo.get("tenant-a", created.id)
    assert unchanged is not None and unchanged.state is RepoCheckoutState.FAILED


# -- cross-tenant isolation (restart-shaped, per tests/security pattern) -------


async def test_reopened_database_hides_repo_rows_from_other_tenants(tmp_path: Path) -> None:
    database_path = tmp_path / "repo-restart.db"
    run_migrations(f"sqlite:///{database_path}")
    first = AsyncSQLiteDatabase(str(database_path))
    try:
        pk = await _seed_parent(first, "tenant-a")
        checkouts = SQLiteRepoCheckoutRepository(first)
        checkout = await checkouts.create(_checkout("tenant-a", pk))
        await checkouts.transition_state(
            "tenant-a",
            checkout.id,
            RepoCheckoutState.STAGED,
            expires_at=utc_now() - timedelta(minutes=1),
        )
        runs = SQLiteRepoRunRepository(first)
        run = await runs.create(
            RepoRun(tenant_id="tenant-a", checkout_id=checkout.id, script_name="ingest")
        )
    finally:
        await first.close()

    restarted = AsyncSQLiteDatabase(str(database_path))
    try:
        checkouts = SQLiteRepoCheckoutRepository(restarted)
        runs = SQLiteRepoRunRepository(restarted)
        # Checkout table: guessing the owner's id from tenant-b reveals nothing.
        assert await checkouts.get("tenant-b", checkout.id) is None
        assert await checkouts.get("tenant-b", "unknown-checkout") is None
        assert await checkouts.list_checkouts("tenant-b") == []
        assert not await checkouts.transition_state(
            "tenant-b", checkout.id, RepoCheckoutState.CONSUMED
        )
        assert await checkouts.expire_stale("tenant-b", now=utc_now()) == []
        # Run table: reads, claims, and fenced writes all stay tenant-bound.
        assert await runs.get("tenant-b", run.id) is None
        assert await runs.get("tenant-b", "unknown-run") is None
        assert await runs.list_runs("tenant-b") == []
        assert await runs.claim_pending("tenant-b", worker_id="foreign-worker") is None
        assert not await runs.finish("tenant-b", run.id, 1, exit_code=0)
        # The owner still sees both rows untouched by the foreign attempts.
        owner_checkout = await checkouts.get("tenant-a", checkout.id)
        assert owner_checkout is not None
        assert owner_checkout.state is RepoCheckoutState.STAGED
        owner_run = await runs.get("tenant-a", run.id)
        assert owner_run is not None
        assert owner_run.state is RepoRunState.PENDING
        assert await checkouts.expire_stale("tenant-a", now=utc_now()) == [checkout.id]
    finally:
        await restarted.close()


async def test_workspace_scoping_hides_rows_from_other_workspaces(sqlite_db) -> None:
    pk = await _seed_parent(sqlite_db, "tenant-a")
    checkouts = SQLiteRepoCheckoutRepository(sqlite_db)
    runs = SQLiteRepoRunRepository(sqlite_db)
    checkout = await checkouts.create(
        _checkout("tenant-a", pk, workspace_id="workspace-a")
    )
    run = await runs.create(
        RepoRun(
            tenant_id="tenant-a",
            workspace_id="workspace-a",
            checkout_id=checkout.id,
            script_name="ingest",
        )
    )

    # workspace-b context cannot see workspace-a rows on either table.
    assert await checkouts.get("tenant-a", checkout.id, workspace_id="workspace-b") is None
    assert await checkouts.list_checkouts("tenant-a", workspace_id="workspace-b") == []
    assert await runs.get("tenant-a", run.id, workspace_id="workspace-b") is None
    assert await runs.list_runs("tenant-a", workspace_id="workspace-b") == []
    assert (
        await runs.claim_pending(
            "tenant-a", worker_id="workspace-b-worker", workspace_id="workspace-b"
        )
        is None
    )
    # The tenant's null-workspace scope does not see workspace rows either.
    assert await checkouts.get("tenant-a", checkout.id) is None
    assert await runs.get("tenant-a", run.id) is None
    # The owning workspace sees and operates on its own rows.
    assert await checkouts.get("tenant-a", checkout.id, workspace_id="workspace-a") is not None
    claimed = await runs.claim_pending(
        "tenant-a", worker_id="workspace-a-worker", workspace_id="workspace-a"
    )
    assert claimed is not None and claimed.run.id == run.id


# -- structural surface validation ---------------------------------------------


def test_repo_surfaces_are_discovered_and_structurally_valid() -> None:
    surfaces = {
        surface.resource_name: surface
        for surface in load_service_persistence_surfaces()
        if surface.resource_name in REPO_RESOURCES
    }
    assert set(surfaces) == set(REPO_RESOURCES)
    for resource_name, surface in surfaces.items():
        definition = SERVICE_SCOPE_REGISTRY.definition_for_resource(resource_name)
        validate_persistence_surface(surface, definition)
        declared = frozenset(
            operation
            for operations in surface.operation_methods.values()
            for operation in operations
        )
        assert declared == definition.operations


def test_repo_repository_module_is_classified_as_persistence() -> None:
    assert "service/repositories/repository.py" in ASYNC_PERSISTENCE_MODULES


def test_repo_resources_are_workspace_scoped() -> None:
    for resource_name in REPO_RESOURCES:
        assert SERVICE_SCOPE_REGISTRY.definition_for_resource(resource_name).workspace_scoped


async def test_every_repo_registry_operation_has_an_executable_probe(tmp_path: Path) -> None:
    """The security matrix picks the new resources up: every case has a probe."""
    surfaces = load_service_persistence_surfaces()
    for resource_name in REPO_RESOURCES:
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


def test_repo_registry_operations_are_the_reviewed_sets() -> None:
    ops = ResourceOperation
    for resource_name in REPO_RESOURCES:
        assert SERVICE_SCOPE_REGISTRY.definition_for_resource(resource_name).operations == {
            ops.CREATE,
            ops.READ,
            ops.ENUMERATE,
            ops.UPDATE,
        }
