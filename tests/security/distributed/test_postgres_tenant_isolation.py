"""Separately addressable tenant-isolation proofs on the real PostgreSQL adapter."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, text

from tests.conftest import requires_docker
from tests.graph.test_models import build_graph
from zeroth.contracts.graph.repository import GraphRepository
from zeroth.contracts.registry import ContractRegistry
from zeroth.governance.audit import AuditQuery, AuditRepository, NodeAuditRecord
from zeroth.governance.approvals import (
    ApprovalDecision,
    ApprovalRecord,
    ApprovalRepository,
    ApprovalService,
)
from zeroth.governance.identity import ActorIdentity, AuthMethod
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.integrations.persistence.runs.checkpoint_store import CheckpointRowStore
from zeroth.platform.storage.json import to_json_value
from zeroth.platform.storage.async_postgres import AsyncPostgresDatabase
from zeroth.platform.storage.scoping import ScopeContext
from zeroth.runtime.runs import Run
from zeroth.service.bootstrap.migrations import run_migrations


def _connection_strings(postgres_container) -> tuple[str, str]:
    url = postgres_container.get_connection_url()
    run_migrations(url.replace("psycopg2", "psycopg"))
    return url.replace("postgresql+psycopg2://", "postgresql://"), uuid4().hex


def _migration_config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "src/zeroth/service/_migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


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
async def test_security_rc_postgres_contract_version_identity_is_tenant_local(
    security_postgres,
) -> None:
    database, unique = security_postgres
    name = f"contract://shared-{unique}"
    tenant_a = ContractRegistry.scoped(
        database,
        ScopeContext(tenant_id="tenant-a", workspace_id="workspace-a"),
    )
    tenant_b = ContractRegistry.scoped(
        database,
        ScopeContext(tenant_id="tenant-b", workspace_id="workspace-b"),
    )

    await tenant_a.register_schema(name, {"type": "string"}, version=1)
    await tenant_b.register_schema(name, {"type": "integer"}, version=1)

    assert (await tenant_a.get(name, 1)).json_schema == {"type": "string"}
    assert (await tenant_b.get(name, 1)).json_schema == {"type": "integer"}


@requires_docker
@pytest.mark.security_rc
async def test_security_rc_postgres_contract_registration_allocates_sequential_versions(
    security_postgres,
) -> None:
    database, unique = security_postgres
    name = f"contract://concurrent-{unique}"
    scope = ScopeContext(tenant_id="tenant-a", workspace_id="workspace-a")
    registries = [ContractRegistry.scoped(database, scope) for _ in range(8)]

    records = await asyncio.gather(
        *(registry.register_schema(name, {"type": "string"}) for registry in registries)
    )

    assert sorted(record.version for record in records) == list(range(1, 9))
    assert [record.version for record in await registries[0].list_versions(name)] == list(
        range(1, 9)
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
        owner = await AuditRepository(restarted).get(f"restart-{unique}", tenant_id="tenant-a")
        assert owner is not None
    finally:
        await restarted.close()


@requires_docker
@pytest.mark.security_rc
async def test_security_rc_postgres_checkpoint_owner_scope(security_postgres) -> None:
    database, unique = security_postgres
    store = CheckpointRowStore(database)
    checkpoint_id = f"security-checkpoint-{unique}"
    owner = _run(f"checkpoint-a-{unique}", tenant_id="tenant-a", suffix=f"a-{unique}")
    foreign = _run(f"checkpoint-b-{unique}", tenant_id="tenant-b", suffix=f"b-{unique}")

    async def write(run: Run) -> None:
        await store.write_row(
            checkpoint_id=checkpoint_id,
            run_id=run.run_id,
            thread_id=run.thread_id,
            tenant_id=run.tenant_id,
            workspace_id=run.workspace_id,
            checkpoint_order=0,
            state_json=to_json_value(run.model_dump(mode="json")),
            created_at=run.updated_at.isoformat(),
        )

    await asyncio.gather(write(owner), write(foreign))
    assert (
        await store.get(checkpoint_id, tenant_id="tenant-a", workspace_id=None)
    ).run_id == owner.run_id
    assert (
        await store.get(checkpoint_id, tenant_id="tenant-b", workspace_id=None)
    ).run_id == foreign.run_id
    assert await store.get(checkpoint_id) is None


@requires_docker
@pytest.mark.security_rc
def test_security_rc_postgres_checkpoint_migration_backfills_owner(postgres_container) -> None:
    database_url = postgres_container.get_connection_url().replace("psycopg2", "psycopg")
    config = _migration_config(database_url)
    command.upgrade(config, "022")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE TABLE run_checkpoints"))
    engine.dispose()
    command.downgrade(config, "021")
    unique = uuid4().hex
    thread_id = f"migration-thread-{unique}"
    checkpoint_id = f"migration-checkpoint-{unique}"
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """INSERT INTO threads (
                    thread_id, graph_version_ref, deployment_ref, status,
                    participating_agent_refs, state_snapshot_refs, checkpoint_refs,
                    memory_bindings, run_ids, created_at, updated_at, tenant_id,
                    workspace_id, workspace_scope
                ) VALUES (:thread_id, 'graph', 'deployment', 'active', '[]', '[]', '[]',
                          '[]', '[]', '2026-08-09', '2026-08-09', 'tenant-a', NULL, 'null')"""
            ),
            {"thread_id": thread_id},
        )
        connection.execute(
            text(
                """INSERT INTO run_checkpoints (
                    checkpoint_id, run_id, thread_id, checkpoint_order, state_json, created_at
                ) VALUES (:checkpoint_id, :run_id, :thread_id, 0, '{}', '2026-08-09')"""
            ),
            {
                "checkpoint_id": checkpoint_id,
                "run_id": f"migration-run-{unique}",
                "thread_id": thread_id,
            },
        )
    engine.dispose()

    command.upgrade(config, "022")
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            owner = connection.execute(
                text(
                    "SELECT tenant_id, workspace_scope FROM run_checkpoints "
                    "WHERE checkpoint_id=:checkpoint_id"
                ),
                {"checkpoint_id": checkpoint_id},
            ).one()
        assert owner == ("tenant-a", "null")
    finally:
        engine.dispose()


@requires_docker
@pytest.mark.security_rc
async def test_security_rc_postgres_approval_opposite_decision_race(security_postgres) -> None:
    database, unique = security_postgres
    repository = ApprovalRepository(database)
    service = ApprovalService(repository=repository, run_repository=RunRepository(database))
    record = await repository.write(
        ApprovalRecord(
            approval_id=f"approval-race-{unique}",
            run_id=f"run-{unique}",
            node_id="approval",
            graph_version_ref="graph:v1",
            deployment_ref="deployment",
            allowed_actions=[ApprovalDecision.APPROVE, ApprovalDecision.REJECT],
            summary="race",
            rationale="race",
        )
    )
    original_get = repository.get
    both_read = asyncio.Event()
    reads = 0
    lock = asyncio.Lock()

    async def synchronized_get(*args, **kwargs):
        nonlocal reads
        result = await original_get(*args, **kwargs)
        async with lock:
            reads += 1
            current = reads
            if reads == 2:
                both_read.set()
        if current <= 2:
            await both_read.wait()
        return result

    repository.get = synchronized_get  # type: ignore[method-assign]
    actor = ActorIdentity(subject="reviewer", auth_method=AuthMethod.API_KEY)
    outcomes = await asyncio.gather(
        service.resolve(record.approval_id, decision=ApprovalDecision.APPROVE, actor=actor),
        service.resolve(record.approval_id, decision=ApprovalDecision.REJECT, actor=actor),
        return_exceptions=True,
    )
    assert sum(isinstance(item, ApprovalRecord) for item in outcomes) == 1
    assert sum(isinstance(item, ValueError) for item in outcomes) == 1
