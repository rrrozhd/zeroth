"""WS-B: THE cross-tenant leak matrix — the falsifiable isolation proof.

Each dimension is exercised at the layer where enforcement actually lives:

* memory (SHARED scope)      — connector/resolver: write as A, read as B -> None
* graphs                     — studio API: foreign-tenant graph_id -> 404
* deployments                — repository: tenant-scoped get/list
* audits                     — AuditQuery: tenant filter on node_audits
* memory connector configs   — repository: A's DSN-bearing ref invisible to B
* runs                       — repository defense-in-depth (the API 404 path is
                               covered by tests/service/test_tenant_isolation.py)

"Write as A / read as B -> denied" is the shape of every case.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from zeroth.integrations.memory.governed.models import MemoryScope

from tests.graph.test_models import build_graph
from tests.service.helpers import approval_resume_graph, deploy_service
from zeroth.governance.audit import AuditQuery, AuditRepository, NodeAuditRecord
from zeroth.service.deployments.repository import SQLiteDeploymentRepository
from zeroth.contracts.graph.repository import GraphRepository
from zeroth.governance.identity import AuthenticatedPrincipal, AuthMethod, ServiceRole
from zeroth.integrations.memory.config_repository import MemoryConnectorConfigRepository
from zeroth.integrations.memory.connectors import KeyValueMemoryConnector
from zeroth.integrations.memory.models import ConnectorManifest
from zeroth.integrations.memory.registry import InMemoryConnectorRegistry, MemoryConnectorResolver
from zeroth.runtime.runs import Run
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.service.api.studio_api import router as studio_router

# ---------------------------------------------------------------------------
# memory — SHARED scope, resolver singleton, one physical connector
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_shared_scope_does_not_leak() -> None:
    raw = KeyValueMemoryConnector()
    registry = InMemoryConnectorRegistry()
    registry.register(
        "memory://kv",
        ConnectorManifest(connector_type="key_value", scope=MemoryScope.SHARED),
        raw,
    )
    resolver = MemoryConnectorResolver(registry=registry, workflow_name="wf")

    a = (await resolver.resolve(["memory://kv"], runtime_context={"tenant_id": "tenant-a"}))[
        0
    ].connector
    b = (await resolver.resolve(["memory://kv"], runtime_context={"tenant_id": "tenant-b"}))[
        0
    ].connector

    await a.write("k", {"v": "a"}, MemoryScope.SHARED)
    assert await b.read("k", MemoryScope.SHARED) is None
    assert await b.search({"text": "a"}, MemoryScope.SHARED) == []


# ---------------------------------------------------------------------------
# graphs — studio API returns 404 (no existence disclosure) across tenants
# ---------------------------------------------------------------------------


def _studio_app(repo: GraphRepository, tenant_id: str) -> FastAPI:
    app = FastAPI()
    bootstrap = type("B", (), {})()
    bootstrap.graph_repository = repo
    bootstrap.audit_repository = None
    app.state.bootstrap = bootstrap
    principal = AuthenticatedPrincipal(
        subject=f"{tenant_id}-admin",
        auth_method=AuthMethod.API_KEY,
        roles=[ServiceRole.ADMIN],
        tenant_id=tenant_id,
    )

    @app.middleware("http")
    async def _inject(request, call_next):
        request.state.principal = principal
        return await call_next(request)

    app.include_router(studio_router)
    return app


@pytest.mark.asyncio
async def test_graph_foreign_tenant_is_404_across_studio_api(sqlite_db) -> None:
    repo = GraphRepository(sqlite_db)
    app_a = _studio_app(repo, "tenant-a")
    app_b = _studio_app(repo, "tenant-b")

    with TestClient(app_a) as client_a, TestClient(app_b) as client_b:
        created = client_a.post("/api/studio/v1/workflows", json={"name": "A's flow"})
        assert created.status_code == 201
        graph_id = created.json()["id"]

        # Owner can read it; the other tenant gets 404 (not 403 — no disclosure).
        assert client_a.get(f"/api/studio/v1/workflows/{graph_id}").status_code == 200
        foreign = client_b.get(f"/api/studio/v1/workflows/{graph_id}")
        assert foreign.status_code == 404

        # And it never appears in tenant B's listing.
        b_list = client_b.get("/api/studio/v1/workflows").json()
        assert all(item["id"] != graph_id for item in b_list)
        # Foreign-tenant publish/delete/clone are equally hidden.
        assert client_b.post(f"/api/studio/v1/workflows/{graph_id}/publish").status_code == 404
        assert client_b.delete(f"/api/studio/v1/workflows/{graph_id}").status_code == 404


@pytest.mark.asyncio
async def test_cloned_draft_stays_owned_by_source_tenant(sqlite_db) -> None:
    # The clone path routes through clone_graph_version + save; a dropped tenant
    # would land the clone under 'default' and leak it to a shared-backend
    # default/other tenant. Prove the clone keeps its source tenant.
    repo = GraphRepository(sqlite_db)
    await repo.save(build_graph().model_copy(update={"graph_id": "g-clone"}), tenant_id="tenant-a")
    await repo.publish("g-clone")
    clone = await repo.clone_published_to_draft("g-clone")

    assert clone.tenant_id == "tenant-a"
    assert await repo.get("g-clone", version=clone.version, tenant_id="tenant-a") is not None
    assert await repo.get("g-clone", version=clone.version, tenant_id="tenant-b") is None
    assert await repo.get("g-clone", version=clone.version, tenant_id="default") is None


# ---------------------------------------------------------------------------
# deployments — repository get/list are tenant-scoped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deployment_repository_is_tenant_scoped(sqlite_db) -> None:
    await deploy_service(
        sqlite_db,
        approval_resume_graph(graph_id="g-dep-a").model_copy(
            update={"tenant_id": "tenant-a", "workspace_id": "workspace-a"}
        ),
        deployment_ref="dep-a",
        tenant_id="tenant-a",
        workspace_id="workspace-a",
    )
    await deploy_service(
        sqlite_db,
        approval_resume_graph(graph_id="g-dep-b").model_copy(
            update={"tenant_id": "tenant-b", "workspace_id": "workspace-b"}
        ),
        deployment_ref="dep-b",
        tenant_id="tenant-b",
        workspace_id="workspace-b",
    )
    repo = SQLiteDeploymentRepository(sqlite_db)

    assert await repo.get("dep-a", tenant_id="tenant-a", workspace_id="workspace-b") is None
    assert await repo.get("dep-a", tenant_id="tenant-a", workspace_id=None) is None
    assert await repo.get("dep-a", tenant_id="tenant-b", workspace_id="workspace-a") is None
    owned = await repo.get("dep-a", tenant_id="tenant-a", workspace_id="workspace-a")
    assert owned is not None
    a_refs = {
        d.deployment_ref for d in await repo.list(tenant_id="tenant-a", workspace_id="workspace-a")
    }
    assert a_refs == {"dep-a"}
    assert await repo.list(tenant_id="tenant-a", workspace_id="workspace-b") == []
    assert await repo.next_version("dep-a", tenant_id="tenant-a", workspace_id="workspace-a") == 2
    assert await repo.next_version("dep-a", tenant_id="tenant-a", workspace_id="workspace-b") == 1

    foreign_collision = owned.model_copy(
        update={
            "deployment_id": "foreign-collision",
            "version": 2,
            "tenant_id": "tenant-b",
            "workspace_id": "workspace-b",
        }
    )
    with pytest.raises(KeyError):
        await repo.create(
            foreign_collision,
            tenant_id="tenant-b",
            workspace_id="workspace-b",
        )
    assert await repo.list("dep-a", tenant_id="tenant-a", workspace_id="workspace-a") == [owned]


# ---------------------------------------------------------------------------
# audits — AuditQuery.tenant_id filters node_audits
# ---------------------------------------------------------------------------


def _audit(audit_id: str, tenant_id: str) -> NodeAuditRecord:
    return NodeAuditRecord(
        audit_id=audit_id,
        run_id=f"run-{tenant_id}",
        node_id="n1",
        graph_version_ref="graph:v1",
        deployment_ref="dep",
        tenant_id=tenant_id,
        status="completed",
        started_at=datetime(2026, 3, 19, tzinfo=UTC),
        completed_at=datetime(2026, 3, 19, 0, 0, 1, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_audit_query_is_tenant_scoped(sqlite_db) -> None:
    repo = AuditRepository(sqlite_db)
    await repo.write(_audit("a1", "tenant-a"))
    await repo.write(_audit("b1", "tenant-b"))

    a_records = await repo.list(AuditQuery(tenant_id="tenant-a"))
    assert {r.audit_id for r in a_records} == {"a1"}
    b_records = await repo.list(AuditQuery(tenant_id="tenant-b"))
    assert {r.audit_id for r in b_records} == {"b1"}


# ---------------------------------------------------------------------------
# memory connector configs — A's DSN-bearing ref invisible to B
# (distinct refs per tenant: ``ref`` is still the PK, so a shared ref would
#  trip an IntegrityError rather than prove READ isolation — see 007 notes.)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connector_config_repository_is_tenant_scoped(sqlite_db) -> None:
    repo = MemoryConnectorConfigRepository(sqlite_db)
    await repo.upsert("kv-a", "key_value", {"dsn": "postgres://a-secret"}, tenant_id="tenant-a")
    await repo.upsert("kv-b", "key_value", {"dsn": "postgres://b-secret"}, tenant_id="tenant-b")

    # Tenant B cannot see or read tenant A's DSN-bearing config.
    assert await repo.get("kv-a", tenant_id="tenant-b") is None
    assert {c.ref for c in await repo.list(tenant_id="tenant-b")} == {"kv-b"}
    assert await repo.delete("kv-a", tenant_id="tenant-b") is False
    # The owner still sees it.
    assert (await repo.get("kv-a", tenant_id="tenant-a")).params == {"dsn": "postgres://a-secret"}


# ---------------------------------------------------------------------------
# runs — repository defense-in-depth (API 404 covered elsewhere)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_repository_optional_tenant_filter(sqlite_db) -> None:
    repo = RunRepository(sqlite_db)
    await repo.create(
        Run(
            run_id="run-a",
            graph_version_ref="graph:v1",
            deployment_ref="dep",
            tenant_id="tenant-a",
        )
    )
    # Foreign tenant cannot fetch it; no-filter (internal) path still can.
    assert await repo.get("run-a", tenant_id="tenant-b") is None
    assert await repo.get("run-a", tenant_id="tenant-a") is not None
    assert await repo.get("run-a") is not None
