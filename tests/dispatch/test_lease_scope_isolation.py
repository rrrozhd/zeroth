"""Composite run identity isolation for the durable lease lifecycle."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI, Request
import pytest

from tests.conftest import requires_docker
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.integrations.persistence.runs.checkpoint_store import CheckpointRowStore
from zeroth.platform.dispatch.lease import LeaseManager
from zeroth.platform.storage.scoping import ScopeContext
from zeroth.runtime.runs import Run, RunFailureState, RunStatus
from zeroth.service.api import admin_api

DEPLOYMENT = "scope-isolation-deployment"
RUN_ID = "shared-run-id"
TENANT_A = "tenant-a"
TENANT_B = "tenant-b"
WORKSPACE_A = "workspace-a"
WORKSPACE_B = "workspace-b"
WORKER_A = "worker-a"
WORKER_B = "worker-b"


def _scope(tenant_id: str, workspace_id: str) -> dict[str, str]:
    return {"tenant_id": tenant_id, "workspace_id": workspace_id}


def _context(tenant_id: str, workspace_id: str) -> ScopeContext:
    return ScopeContext(tenant_id=tenant_id, workspace_id=workspace_id)


def _run(tenant_id: str, workspace_id: str) -> Run:
    return Run(
        run_id=RUN_ID,
        thread_id=f"thread-{tenant_id}-{workspace_id}",
        graph_version_ref="graph:v1",
        deployment_ref=DEPLOYMENT,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
    )


async def _create_colliding_runs(
    database,
    foreign_tenant: str,
    foreign_workspace: str,
) -> tuple[RunRepository, RunRepository]:
    owner = RunRepository(database, _context(TENANT_A, WORKSPACE_A))
    foreign = RunRepository(database, _context(foreign_tenant, foreign_workspace))
    await owner.create(_run(TENANT_A, WORKSPACE_A))
    await foreign.create(_run(foreign_tenant, foreign_workspace))
    return owner, foreign


async def _row(database, tenant_id: str, workspace_id: str) -> dict[str, object]:
    async with database.transaction() as connection:
        row = await connection.fetch_one(
            """SELECT status, current_step, lease_worker_id, lease_generation,
                      lease_expires_at, recovery_checkpoint_id, failure_state,
                      failure_count
               FROM runs
               WHERE tenant_id = ? AND workspace_scope = ? AND run_id = ?""",
            (tenant_id, f"value:{workspace_id}", RUN_ID),
        )
    assert row is not None
    return dict(row)


async def _lease_both(database, *, checkpoint: bool = False) -> None:
    async with database.transaction() as connection:
        await connection.execute(
            """UPDATE runs
               SET lease_worker_id = ?,
                   lease_acquired_at = ?,
                   lease_expires_at = ?,
                   recovery_checkpoint_id = ?
               WHERE run_id = ?""",
            (
                WORKER_A,
                "2030-01-01T00:00:00+00:00",
                "2030-01-01T00:01:00+00:00",
                "held-checkpoint" if checkpoint else None,
                RUN_ID,
            ),
        )


async def _lease_one(
    database,
    tenant_id: str,
    workspace_id: str,
    *,
    expires_at: str,
) -> None:
    async with database.transaction() as connection:
        await connection.execute(
            """UPDATE runs
               SET lease_worker_id = ?, lease_generation = 1,
                   lease_acquired_at = ?, lease_expires_at = ?
               WHERE tenant_id = ? AND workspace_scope = ? AND run_id = ?""",
            (
                WORKER_A,
                "2030-01-01T00:00:00+00:00",
                expires_at,
                tenant_id,
                f"value:{workspace_id}",
                RUN_ID,
            ),
        )


@pytest.fixture(
    params=(
        pytest.param((TENANT_B, WORKSPACE_A), id="cross-tenant"),
        pytest.param((TENANT_A, WORKSPACE_B), id="cross-workspace"),
    )
)
def foreign_scope(request) -> tuple[str, str]:
    return request.param


@pytest.fixture(
    params=(
        pytest.param({}, id="omitted"),
        pytest.param({"tenant_id": TENANT_A}, id="partial"),
    )
)
def legacy_scope(request) -> dict[str, str]:
    return request.param


@requires_docker
@pytest.mark.security_rc
async def test_duplicate_run_id_lease_lifecycle_is_scope_isolated(
    dual_database,
    foreign_scope: tuple[str, str],
) -> None:
    foreign_tenant, foreign_workspace = foreign_scope
    await _create_colliding_runs(dual_database, foreign_tenant, foreign_workspace)
    manager = LeaseManager(dual_database)

    assert (
        await manager.claim_pending(DEPLOYMENT, WORKER_A, **_scope(TENANT_A, WORKSPACE_A)) == RUN_ID
    )
    owner = await _row(dual_database, TENANT_A, WORKSPACE_A)
    foreign = await _row(dual_database, foreign_tenant, foreign_workspace)
    assert owner["lease_worker_id"] == WORKER_A
    assert owner["lease_generation"] == 1
    assert foreign["lease_worker_id"] is None
    assert foreign["lease_generation"] == 0

    assert await manager.current_holder(RUN_ID, **_scope(TENANT_A, WORKSPACE_A)) == WORKER_A
    assert await manager.current_holder(RUN_ID, **_scope(foreign_tenant, foreign_workspace)) is None
    assert await manager.current_generation(RUN_ID, **_scope(TENANT_A, WORKSPACE_A)) == 1
    assert (
        await manager.current_generation(RUN_ID, **_scope(foreign_tenant, foreign_workspace)) == 0
    )
    foreign_expiry = "2035-01-01T00:00:00+00:00"
    await _lease_one(
        dual_database,
        foreign_tenant,
        foreign_workspace,
        expires_at=foreign_expiry,
    )
    assert await manager.renew_lease(
        RUN_ID,
        WORKER_A,
        generation=1,
        **_scope(TENANT_A, WORKSPACE_A),
    )
    assert (await _row(dual_database, foreign_tenant, foreign_workspace))[
        "lease_expires_at"
    ] == foreign_expiry
    assert await manager.commit_fenced(
        RUN_ID,
        WORKER_A,
        generation=1,
        current_step="owner-step",
        **_scope(TENANT_A, WORKSPACE_A),
    )
    assert (await _row(dual_database, TENANT_A, WORKSPACE_A))["current_step"] == "owner-step"
    assert (await _row(dual_database, foreign_tenant, foreign_workspace))["current_step"] is None

    await _lease_both(dual_database)
    await manager.release_lease(
        RUN_ID,
        WORKER_A,
        generation=1,
        **_scope(TENANT_A, WORKSPACE_A),
    )
    assert (await _row(dual_database, TENANT_A, WORKSPACE_A))["lease_worker_id"] is None
    assert (await _row(dual_database, foreign_tenant, foreign_workspace))[
        "lease_worker_id"
    ] == WORKER_A

    await _lease_both(dual_database, checkpoint=True)
    await manager.clear_lease(RUN_ID, **_scope(TENANT_A, WORKSPACE_A))
    owner = await _row(dual_database, TENANT_A, WORKSPACE_A)
    foreign = await _row(dual_database, foreign_tenant, foreign_workspace)
    assert owner["lease_worker_id"] is None
    assert owner["recovery_checkpoint_id"] is None
    assert foreign["lease_worker_id"] == WORKER_A
    assert foreign["recovery_checkpoint_id"] == "held-checkpoint"


@requires_docker
@pytest.mark.security_rc
async def test_ambiguous_legacy_run_id_lifecycle_fails_closed(
    dual_database,
    foreign_scope: tuple[str, str],
) -> None:
    foreign_tenant, foreign_workspace = foreign_scope
    await _create_colliding_runs(dual_database, foreign_tenant, foreign_workspace)
    await _lease_both(dual_database, checkpoint=True)
    manager = LeaseManager(dual_database)

    assert await manager.current_holder(RUN_ID) is None
    assert await manager.current_generation(RUN_ID) is None
    assert await manager.renew_lease(RUN_ID, WORKER_A, generation=0) is False
    assert (
        await manager.commit_fenced(RUN_ID, WORKER_A, generation=0, current_step="ambiguous")
        is False
    )
    assert await manager.get_recovery_checkpoint_id(RUN_ID) is None
    await manager.release_lease(RUN_ID, WORKER_A, generation=1)
    await manager.clear_lease(RUN_ID)

    owner = await _row(dual_database, TENANT_A, WORKSPACE_A)
    foreign = await _row(dual_database, foreign_tenant, foreign_workspace)
    assert owner["lease_worker_id"] == foreign["lease_worker_id"] == WORKER_A
    assert owner["current_step"] is foreign["current_step"] is None
    assert owner["recovery_checkpoint_id"] == "held-checkpoint"
    assert foreign["recovery_checkpoint_id"] == "held-checkpoint"


@requires_docker
@pytest.mark.security_rc
async def test_omitted_or_partial_scope_cannot_select_unique_workspace_run(
    dual_database,
    legacy_scope: dict[str, str],
) -> None:
    repository = RunRepository(dual_database, _context(TENANT_A, WORKSPACE_A))
    await repository.create(_run(TENANT_A, WORKSPACE_A))
    manager = LeaseManager(dual_database)

    assert await manager.claim_pending(DEPLOYMENT, WORKER_A, **legacy_scope) is None
    await _lease_one(
        dual_database,
        TENANT_A,
        WORKSPACE_A,
        expires_at="2035-01-01T00:00:00+00:00",
    )
    assert await manager.current_holder(RUN_ID, **legacy_scope) is None
    assert await manager.current_generation(RUN_ID, **legacy_scope) is None
    assert await manager.renew_lease(RUN_ID, WORKER_A, generation=1, **legacy_scope) is False
    assert (
        await manager.commit_fenced(
            RUN_ID,
            WORKER_A,
            generation=1,
            current_step="legacy",
            **legacy_scope,
        )
        is False
    )
    assert await manager.get_recovery_checkpoint_id(RUN_ID, **legacy_scope) is None
    await manager.release_lease(RUN_ID, WORKER_A, generation=1, **legacy_scope)
    await manager.clear_lease(RUN_ID, **legacy_scope)
    row = await _row(dual_database, TENANT_A, WORKSPACE_A)
    assert row["lease_worker_id"] == WORKER_A
    assert row["current_step"] is None

    await repository.transition(RUN_ID, RunStatus.RUNNING)
    async with dual_database.transaction() as connection:
        await connection.execute(
            """UPDATE runs SET lease_expires_at = ?
               WHERE tenant_id = ? AND workspace_scope = ? AND run_id = ?""",
            (
                "2000-01-01T00:00:00+00:00",
                TENANT_A,
                f"value:{WORKSPACE_A}",
                RUN_ID,
            ),
        )
    assert await manager.claim_orphaned(DEPLOYMENT, WORKER_B, **legacy_scope) == []


@requires_docker
@pytest.mark.security_rc
async def test_duplicate_run_id_orphan_recovery_is_scope_isolated(
    dual_database,
    foreign_scope: tuple[str, str],
) -> None:
    foreign_tenant, foreign_workspace = foreign_scope
    owner_repository, foreign_repository = await _create_colliding_runs(
        dual_database, foreign_tenant, foreign_workspace
    )
    await owner_repository.transition(RUN_ID, RunStatus.RUNNING)
    await foreign_repository.transition(RUN_ID, RunStatus.RUNNING)
    for tenant_id, workspace_id, checkpoint_id, checkpoint_order in (
        (TENANT_A, WORKSPACE_A, "owner-checkpoint", 10),
        (foreign_tenant, foreign_workspace, "foreign-checkpoint", 20),
    ):
        store = CheckpointRowStore(dual_database, _context(tenant_id, workspace_id))
        await store.write_row(
            checkpoint_id=checkpoint_id,
            run_id=RUN_ID,
            thread_id=f"thread-{tenant_id}-{workspace_id}",
            checkpoint_order=checkpoint_order,
            state_json="{}",
            created_at=datetime(2030, 1, 1, tzinfo=UTC).isoformat(),
        )
    async with dual_database.transaction() as connection:
        await connection.execute(
            """UPDATE runs
               SET lease_worker_id = ?, lease_expires_at = ?
               WHERE run_id = ?""",
            (WORKER_A, "2000-01-01T00:00:00+00:00", RUN_ID),
        )

    manager = LeaseManager(dual_database)
    assert await manager.claim_orphaned(
        DEPLOYMENT,
        WORKER_B,
        **_scope(TENANT_A, WORKSPACE_A),
    ) == [RUN_ID]
    assert (
        await manager.get_recovery_checkpoint_id(RUN_ID, **_scope(TENANT_A, WORKSPACE_A))
        == "owner-checkpoint"
    )
    owner = await _row(dual_database, TENANT_A, WORKSPACE_A)
    foreign = await _row(dual_database, foreign_tenant, foreign_workspace)
    assert owner["lease_worker_id"] == WORKER_B
    assert owner["recovery_checkpoint_id"] == "owner-checkpoint"
    assert foreign["lease_worker_id"] == WORKER_A
    assert foreign["recovery_checkpoint_id"] is None


@requires_docker
@pytest.mark.security_rc
async def test_duplicate_run_id_admin_cancel_clears_only_its_scope(
    dual_database,
    monkeypatch,
    foreign_scope: tuple[str, str],
) -> None:
    foreign_tenant, foreign_workspace = foreign_scope
    owner_repository, _ = await _create_colliding_runs(
        dual_database, foreign_tenant, foreign_workspace
    )
    await _lease_both(dual_database)
    lease_manager = LeaseManager(dual_database)
    deployment = SimpleNamespace(
        deployment_ref=DEPLOYMENT,
        tenant_id=TENANT_A,
        workspace_id=WORKSPACE_A,
    )
    app = FastAPI()
    admin_api.register_admin_routes(app)
    app.state.bootstrap = SimpleNamespace(
        deployment=deployment,
        run_repository=owner_repository,
        lease_manager=lease_manager,
    )
    monkeypatch.setattr(admin_api, "require_permission", AsyncMock())
    monkeypatch.setattr(admin_api, "require_deployment_scope", AsyncMock())
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"/admin/runs/{RUN_ID}/cancel",
            "headers": [],
            "app": app,
        }
    )
    cancel = next(route.endpoint for route in app.routes if route.name == "cancel_run")

    response = await cancel(request, RUN_ID)

    assert response.status.value == "failed"
    owner = await _row(dual_database, TENANT_A, WORKSPACE_A)
    foreign = await _row(dual_database, foreign_tenant, foreign_workspace)
    assert owner["lease_worker_id"] is None
    assert foreign["status"] == RunStatus.PENDING.value
    assert foreign["lease_worker_id"] == WORKER_A


@requires_docker
@pytest.mark.security_rc
async def test_duplicate_run_id_admin_replay_requeues_only_its_scope(
    dual_database,
    monkeypatch,
    foreign_scope: tuple[str, str],
) -> None:
    foreign_tenant, foreign_workspace = foreign_scope
    owner_repository, foreign_repository = await _create_colliding_runs(
        dual_database, foreign_tenant, foreign_workspace
    )
    for repository in (owner_repository, foreign_repository):
        await repository.transition(
            RUN_ID,
            RunStatus.FAILED,
            failure_state=RunFailureState(reason="dead_letter", message="boom"),
        )
    await _lease_both(dual_database, checkpoint=True)
    async with dual_database.transaction() as connection:
        await connection.execute(
            "UPDATE runs SET failure_count = 3 WHERE run_id = ?",
            (RUN_ID,),
        )

    deployment = SimpleNamespace(
        deployment_ref=DEPLOYMENT,
        tenant_id=TENANT_A,
        workspace_id=WORKSPACE_A,
    )
    app = FastAPI()
    app.state.bootstrap = SimpleNamespace(
        deployment=deployment,
        run_repository=owner_repository,
    )
    monkeypatch.setattr(admin_api, "require_permission", AsyncMock())
    monkeypatch.setattr(admin_api, "require_deployment_scope", AsyncMock())
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"/admin/runs/{RUN_ID}/replay",
            "headers": [],
            "app": app,
        }
    )

    response = await admin_api._replay_failed_run(request, RUN_ID)

    assert response.status.value == "queued"
    owner = await _row(dual_database, TENANT_A, WORKSPACE_A)
    foreign = await _row(dual_database, foreign_tenant, foreign_workspace)
    assert owner["failure_state"] is None
    assert owner["failure_count"] == 0
    assert owner["lease_worker_id"] is None
    assert foreign["status"] == RunStatus.FAILED.value
    assert foreign["failure_state"] is not None
    assert foreign["failure_count"] == 3
    assert foreign["lease_worker_id"] == WORKER_A
