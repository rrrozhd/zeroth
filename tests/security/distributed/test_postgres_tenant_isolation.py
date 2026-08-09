"""Separately addressable tenant-isolation proofs on the real PostgreSQL adapter."""

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


def _connection_strings(postgres_container) -> tuple[str, str]:
    url = postgres_container.get_connection_url()
    run_migrations(url.replace("psycopg2", "psycopg"))
    return url.replace("postgresql+psycopg2://", "postgresql://"), uuid4().hex


@pytest.fixture
async def security_postgres(postgres_container):
    dsn, unique = _connection_strings(postgres_container)
    database = await AsyncPostgresDatabase.create(dsn, min_size=1, max_size=3)
    try:
        yield database, unique
    finally:
        await database.close()


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
async def test_security_rc_postgres_graph_scope_predicate(security_postgres) -> None:
    database, unique = security_postgres
    repository = GraphRepository(database)
    graph_id = f"security-graph-{unique}"
    await repository.save(
        build_graph().model_copy(update={"graph_id": graph_id}), tenant_id="tenant-a"
    )
    assert await repository.get(graph_id, tenant_id="tenant-b") is None
    assert await repository.get("unknown-graph", tenant_id="tenant-b") is None


@requires_docker
@pytest.mark.security_rc
async def test_security_rc_postgres_run_scope_predicate(security_postgres) -> None:
    database, unique = security_postgres
    repository = RunRepository(database)
    run_id = f"security-run-scope-{unique}"
    await repository.create(_run(run_id, tenant_id="tenant-a", suffix=unique))
    assert await repository.get(run_id, tenant_id="tenant-b") is None
    assert await repository.get("unknown-run", tenant_id="tenant-b") is None


@requires_docker
@pytest.mark.security_rc
async def test_security_rc_postgres_audit_scope_predicate(security_postgres) -> None:
    database, unique = security_postgres
    repository = AuditRepository(database)
    await repository.write(_audit(f"a-{unique}", tenant_id="tenant-a"))
    await repository.write(_audit(f"b-{unique}", tenant_id="tenant-b"))
    assert {
        record.audit_id for record in await repository.list(AuditQuery(tenant_id="tenant-a"))
    } == {f"a-{unique}"}


@requires_docker
@pytest.mark.security_rc
async def test_security_rc_postgres_same_id_tenant_race(security_postgres) -> None:
    database, unique = security_postgres
    run_id = f"security-run-race-{unique}"
    raced = await asyncio.gather(
        RunRepository(database).create(_run(run_id, tenant_id="tenant-a", suffix=f"a-{unique}")),
        RunRepository(database).create(_run(run_id, tenant_id="tenant-b", suffix=f"b-{unique}")),
        return_exceptions=True,
    )
    assert sum(isinstance(result, Run) for result in raced) == 1
    assert sum(isinstance(result, KeyError) for result in raced) == 1
    winner = await RunRepository(database).get(run_id)
    assert winner is not None
    loser = "tenant-b" if winner.tenant_id == "tenant-a" else "tenant-a"
    assert await RunRepository(database).get(run_id, tenant_id=loser) is None


@requires_docker
@pytest.mark.security_rc
async def test_security_rc_postgres_pool_restart_preserves_scope(postgres_container) -> None:
    dsn, unique = _connection_strings(postgres_container)
    graph_id = f"restart-graph-{unique}"
    run_id = f"restart-run-{unique}"
    first = await AsyncPostgresDatabase.create(dsn, min_size=1, max_size=2)
    try:
        await GraphRepository(first).save(
            build_graph().model_copy(update={"graph_id": graph_id}), tenant_id="tenant-a"
        )
        await RunRepository(first).create(_run(run_id, tenant_id="tenant-a", suffix=unique))
        await AuditRepository(first).write(_audit(f"restart-{unique}", tenant_id="tenant-a"))
    finally:
        await first.close()

    restarted = await AsyncPostgresDatabase.create(dsn, min_size=1, max_size=2)
    try:
        assert await GraphRepository(restarted).get(graph_id, tenant_id="tenant-b") is None
        assert await RunRepository(restarted).get(run_id, tenant_id="tenant-b") is None
        foreign_ids = {
            record.audit_id
            for record in await AuditRepository(restarted).list(AuditQuery(tenant_id="tenant-b"))
        }
        assert f"restart-{unique}" not in foreign_ids
        owner = await AuditRepository(restarted).get(
            f"restart-{unique}", tenant_id="tenant-a"
        )
        assert owner is not None
    finally:
        await restarted.close()
