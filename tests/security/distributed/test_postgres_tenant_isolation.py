"""Real-PostgreSQL tenant predicates, collision, and pool-restart proof."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from tests.conftest import requires_docker
from tests.graph.test_models import build_graph
from zeroth.contracts.graph.repository import GraphRepository
from zeroth.governance.audit import AuditQuery, AuditRepository, NodeAuditRecord
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.platform.storage.async_postgres import AsyncPostgresDatabase
from zeroth.runtime.runs import Run
from zeroth.service.bootstrap.migrations import run_migrations


def _run(run_id: str, *, tenant_id: str, suffix: str) -> Run:
    return Run(
        run_id=run_id,
        thread_id=f"thread-{suffix}",
        graph_version_ref=f"graph-{suffix}:v1",
        deployment_ref=f"deployment-{suffix}",
        tenant_id=tenant_id,
    )


def _audit(audit_id: str, *, tenant_id: str) -> NodeAuditRecord:
    return NodeAuditRecord(
        audit_id=audit_id,
        run_id=f"run-{audit_id}",
        node_id="security-node",
        graph_version_ref="graph:v1",
        deployment_ref="deployment",
        tenant_id=tenant_id,
        status="completed",
        started_at=datetime(2026, 8, 9, tzinfo=UTC),
        completed_at=datetime(2026, 8, 9, 0, 0, 1, tzinfo=UTC),
    )


@requires_docker
@pytest.mark.security_rc
async def test_security_rc_postgres_predicates_race_and_pool_restart(postgres_container) -> None:
    unique = uuid4().hex
    url = postgres_container.get_connection_url()
    migration_url = url.replace("psycopg2", "psycopg")
    dsn = url.replace("postgresql+psycopg2://", "postgresql://")
    run_migrations(migration_url)
    first = await AsyncPostgresDatabase.create(dsn, min_size=1, max_size=3)
    try:
        graph_repo = GraphRepository(first)
        graph_id = f"security-graph-{unique}"
        await graph_repo.save(
            build_graph().model_copy(update={"graph_id": graph_id}), tenant_id="tenant-a"
        )
        assert await graph_repo.get(graph_id, tenant_id="tenant-a") is not None
        assert await graph_repo.get(graph_id, tenant_id="tenant-b") is None

        run_repo_a = RunRepository(first)
        run_repo_b = RunRepository(first)
        run_id = f"security-run-{unique}"
        raced = await asyncio.gather(
            run_repo_a.create(_run(run_id, tenant_id="tenant-a", suffix=f"a-{unique}")),
            run_repo_b.create(_run(run_id, tenant_id="tenant-b", suffix=f"b-{unique}")),
            return_exceptions=True,
        )
        assert sum(isinstance(result, Run) for result in raced) == 1
        assert sum(isinstance(result, KeyError) for result in raced) == 1
        winner = await run_repo_a.get(run_id)
        assert winner is not None
        loser = "tenant-b" if winner.tenant_id == "tenant-a" else "tenant-a"
        assert await run_repo_a.get(run_id, tenant_id=loser) is None

        audit_repo = AuditRepository(first)
        await audit_repo.write(_audit(f"a-{unique}", tenant_id="tenant-a"))
        await audit_repo.write(_audit(f"b-{unique}", tenant_id="tenant-b"))
        assert {
            record.audit_id for record in await audit_repo.list(AuditQuery(tenant_id="tenant-a"))
        } == {f"a-{unique}"}
    finally:
        await first.close()

    restarted = await AsyncPostgresDatabase.create(dsn, min_size=1, max_size=2)
    try:
        assert await GraphRepository(restarted).get(graph_id, tenant_id="tenant-b") is None
        assert await RunRepository(restarted).get(run_id, tenant_id=loser) is None
        assert {
            record.audit_id
            for record in await AuditRepository(restarted).list(AuditQuery(tenant_id="tenant-b"))
            if record.audit_id.endswith(unique)
        } == {f"b-{unique}"}
    finally:
        await restarted.close()
