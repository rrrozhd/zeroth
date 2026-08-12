"""Restart, worker, and concurrency proofs across durable tenant boundaries."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from pathlib import Path

from tests.service.helpers import approval_resume_graph, deploy_service
from zeroth.governance.audit import AuditQuery, AuditRepository, NodeAuditRecord
from zeroth.integrations.memory.config_repository import MemoryConnectorConfigRepository
from zeroth.integrations.persistence.runs import RunRepository, ThreadRepository
from zeroth.platform.dispatch import LeaseManager
from zeroth.platform.storage.async_sqlite import AsyncSQLiteDatabase
from zeroth.platform.storage import NullWorkspaceScopeContext
from zeroth.runtime.runs import Run, RunStatus
from zeroth.runtime.orchestration.run_worker import RunWorker
from zeroth.service.bootstrap.migrations import run_migrations
from zeroth.service.deployments.repository import SQLiteDeploymentRepository


def _run(run_id: str, *, tenant_id: str, thread_id: str, deployment_ref: str = "deployment") -> Run:
    return Run(
        run_id=run_id,
        thread_id=thread_id,
        graph_version_ref="graph:v1",
        deployment_ref=deployment_ref,
        tenant_id=tenant_id,
    )


def _audit(audit_id: str, tenant_id: str) -> NodeAuditRecord:
    return NodeAuditRecord(
        audit_id=audit_id,
        run_id=f"run-{tenant_id}",
        node_id="node",
        graph_version_ref="graph:v1",
        deployment_ref="deployment",
        tenant_id=tenant_id,
        status="completed",
        started_at=datetime(2026, 8, 9, tzinfo=UTC),
        completed_at=datetime(2026, 8, 9, 0, 0, 1, tzinfo=UTC),
    )


async def test_sqlite_repository_reconstruction_preserves_scope_predicates(tmp_path: Path) -> None:
    database_path = tmp_path / "security-restart.db"
    run_migrations(f"sqlite:///{database_path}")
    first = AsyncSQLiteDatabase(str(database_path))
    try:
        await RunRepository(first).create(
            _run("owner-run", tenant_id="tenant-a", thread_id="owner-thread")
        )
        await AuditRepository.scoped(first, NullWorkspaceScopeContext(tenant_id="tenant-a")).write(
            _audit("owner-audit", "tenant-a")
        )
        await MemoryConnectorConfigRepository(first).upsert(
            "owner-connector",
            "key_value",
            {"dsn": "postgres://owner-secret"},
            tenant_id="tenant-a",
        )
    finally:
        await first.close()

    restarted = AsyncSQLiteDatabase(str(database_path))
    try:
        assert await RunRepository(restarted).get("owner-run", tenant_id="tenant-b") is None
        assert await ThreadRepository(restarted).get("owner-thread", tenant_id="tenant-b") is None
        assert (
            await AuditRepository.scoped(
                restarted, NullWorkspaceScopeContext(tenant_id="tenant-b")
            ).list(AuditQuery(tenant_id="tenant-b"))
            == []
        )
        assert (
            await MemoryConnectorConfigRepository(restarted).get(
                "owner-connector", tenant_id="tenant-b"
            )
            is None
        )
    finally:
        await restarted.close()


async def test_execution_result_guessing_stays_hidden_after_restart(tmp_path: Path) -> None:
    database_path = tmp_path / "result-restart.db"
    run_migrations(f"sqlite:///{database_path}")
    first = AsyncSQLiteDatabase(str(database_path))
    owner = _run("result-run", tenant_id="tenant-a", thread_id="result-thread")
    owner.status = RunStatus.COMPLETED
    owner.final_output = {"secret_result": "tenant-a-only"}
    await RunRepository(first).create(owner)
    await first.close()

    restarted = AsyncSQLiteDatabase(str(database_path))
    try:
        foreign = await RunRepository(restarted).get("result-run", tenant_id="tenant-b")
        unknown = await RunRepository(restarted).get("unknown-run", tenant_id="tenant-b")
        assert foreign is unknown is None
    finally:
        await restarted.close()


async def test_sqlite_deployment_repository_reconstruction_preserves_scope(tmp_path: Path) -> None:
    database_path = tmp_path / "deployment-restart.db"
    run_migrations(f"sqlite:///{database_path}")
    first = AsyncSQLiteDatabase(str(database_path))
    await deploy_service(
        first,
        approval_resume_graph(graph_id="restart-deployment-graph").model_copy(
            update={"tenant_id": "tenant-a", "workspace_id": "workspace-a"}
        ),
        deployment_ref="restart-deployment",
        tenant_id="tenant-a",
        workspace_id="workspace-a",
    )
    await first.close()

    restarted = AsyncSQLiteDatabase(str(database_path))
    try:
        repository = SQLiteDeploymentRepository(restarted)
        foreign = await repository.get(
            "restart-deployment", tenant_id="tenant-b", workspace_id="workspace-b"
        )
        unknown = await repository.get(
            "unknown-deployment", tenant_id="tenant-b", workspace_id="workspace-b"
        )
        assert foreign is unknown is None
        assert await repository.list(tenant_id="tenant-b", workspace_id="workspace-b") == []
    finally:
        await restarted.close()


async def test_checkpoint_guessing_is_hidden_by_owning_run_tenant(sqlite_db) -> None:
    repository = RunRepository(sqlite_db)
    owner = await repository.create(
        _run("checkpoint-run", tenant_id="tenant-a", thread_id="checkpoint-thread")
    )
    assert owner.checkpoint_id is not None

    foreign = await repository.get_checkpoint(owner.checkpoint_id, tenant_id="tenant-b")
    unknown = await repository.get_checkpoint("unknown-checkpoint", tenant_id="tenant-b")

    assert foreign is unknown is None


async def test_worker_claims_only_its_tenant_pending_runs(sqlite_db) -> None:
    repository = RunRepository(sqlite_db)
    await repository.create(
        _run(
            "tenant-a-run",
            tenant_id="tenant-a",
            thread_id="tenant-a-thread",
            deployment_ref="shared-deployment-ref",
        )
    )
    await repository.create(
        _run(
            "tenant-b-run",
            tenant_id="tenant-b",
            thread_id="tenant-b-thread",
            deployment_ref="shared-deployment-ref",
        )
    )
    leases = LeaseManager(sqlite_db)

    claimed = await leases.claim_pending(
        "shared-deployment-ref", "tenant-a-worker", tenant_id="tenant-a"
    )
    no_foreign_claim = await leases.claim_pending(
        "shared-deployment-ref", "tenant-a-worker-2", tenant_id="tenant-a"
    )

    assert claimed == "tenant-a-run"
    assert no_foreign_claim is None
    foreign = await repository.get("tenant-b-run")
    assert foreign is not None
    assert foreign.status is RunStatus.PENDING


async def test_restarted_dispatch_worker_executes_only_its_deployment_tenant(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "worker-restart.db"
    run_migrations(f"sqlite:///{database_path}")
    first = AsyncSQLiteDatabase(str(database_path))
    repository = RunRepository(first)
    for tenant in ("tenant-a", "tenant-b"):
        await repository.create(
            _run(
                f"{tenant}-worker-run",
                tenant_id=tenant,
                thread_id=f"{tenant}-worker-thread",
                deployment_ref="restarted-worker-deployment",
            )
        )
    await first.close()

    restarted = AsyncSQLiteDatabase(str(database_path))
    repository = RunRepository(restarted)
    leases = LeaseManager(restarted)
    transition_committed = asyncio.Event()

    class _Orchestrator:
        def __init__(self) -> None:
            self.driven: list[str] = []

        async def _drive(self, _graph, run) -> Run:
            self.driven.append(run.run_id)
            completed = await repository.transition(run.run_id, RunStatus.COMPLETED)
            transition_committed.set()
            return completed

    orchestrator = _Orchestrator()
    worker = RunWorker(
        deployment_ref="restarted-worker-deployment",
        tenant_id="tenant-a",
        workspace_id=None,
        run_repository=repository,
        orchestrator=orchestrator,
        graph=object(),
        lease_manager=leases,
        poll_interval=0.01,
    )
    task = asyncio.create_task(worker.poll_loop())
    try:
        await asyncio.wait_for(transition_committed.wait(), timeout=2)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert orchestrator.driven == ["tenant-a-worker-run"]
    tenant_a = await repository.get("tenant-a-worker-run")
    tenant_b = await repository.get("tenant-b-worker-run")
    assert tenant_a is not None and tenant_a.status is RunStatus.COMPLETED
    assert tenant_b is not None and tenant_b.status is RunStatus.PENDING
    await restarted.close()
