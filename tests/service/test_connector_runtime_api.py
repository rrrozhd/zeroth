"""Tests for runtime memory connector management (POST/PUT/DELETE/test)."""

from __future__ import annotations

from fastapi.testclient import TestClient
from zeroth.core.governed.memory.models import MemoryScope

from tests.service.helpers import (
    agent_graph,
    deploy_service,
    operator_headers,
    reviewer_headers,
)
from zeroth.core.memory.config_repository import MemoryConnectorConfigRepository
from zeroth.core.memory.registry import InMemoryConnectorRegistry
from zeroth.core.memory.runtime_configs import load_persisted_connectors
from zeroth.core.service.bootstrap import bootstrap_app

DEPLOYMENT = "connectors-runtime-test"

PG_PARAMS = {"dsn": "postgresql://zeroth:s3cret@127.0.0.1:5499/zeroth_live"}
PG_MASKED_DSN = "postgresql://***@127.0.0.1:5499/zeroth_live"
REDIS_PARAMS = {"url": "redis://user:s3cret@127.0.0.1:6399/0", "key_prefix": "probe:kv"}
REDIS_MASKED_URL = "redis://***@127.0.0.1:6399/0"


async def _app(sqlite_db, suffix: str):
    service, _ = await deploy_service(
        sqlite_db,
        agent_graph(graph_id=f"graph-conn-rt-{suffix}"),
        deployment_ref=f"{DEPLOYMENT}-{suffix}",
    )
    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service
    return app, service


async def test_post_creates_and_lists_with_masked_params(sqlite_db) -> None:
    app, service = await _app(sqlite_db, "create")

    with TestClient(app) as client:
        r = client.post(
            "/v1/connectors",
            json={"ref": "live-vectors", "backend_type": "pgvector", "params": PG_PARAMS},
            headers=operator_headers(),
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["ref"] == "live-vectors"
        assert body["source"] == "runtime"
        assert body["backend_type"] == "pgvector"
        assert body["params"]["dsn"] == PG_MASKED_DSN
        assert "s3cret" not in r.text

        listing = client.get("/v1/connectors", headers=operator_headers())

    assert listing.status_code == 200
    by_ref = {c["ref"]: c for c in listing.json()}
    assert by_ref["live-vectors"]["source"] == "runtime"
    assert by_ref["live-vectors"]["params"]["dsn"] == PG_MASKED_DSN
    # Env-sourced connectors expose no params and stay marked "env".
    assert by_ref["key_value"]["source"] == "env"
    assert by_ref["key_value"]["params"] is None
    # The live registry resolves the new ref immediately (next run can use it).
    manifest, _ = service.memory_registry.resolve("live-vectors")
    assert manifest.connector_type == "pgvector"


async def test_post_redis_masks_url(sqlite_db) -> None:
    app, _ = await _app(sqlite_db, "redis")

    with TestClient(app) as client:
        r = client.post(
            "/v1/connectors",
            json={"ref": "cache-kv", "backend_type": "redis_kv", "params": REDIS_PARAMS},
            headers=operator_headers(),
        )

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["params"]["url"] == REDIS_MASKED_URL
    assert body["params"]["key_prefix"] == "probe:kv"
    assert body["scope"] == "shared"


async def test_post_env_ref_collision_conflicts(sqlite_db) -> None:
    app, _ = await _app(sqlite_db, "envdup")

    with TestClient(app) as client:
        r = client.post(
            "/v1/connectors",
            json={"ref": "key_value", "backend_type": "pgvector", "params": PG_PARAMS},
            headers=operator_headers(),
        )

    assert r.status_code == 409
    assert "env-sourced" in r.json()["detail"]


async def test_post_duplicate_runtime_ref_conflicts(sqlite_db) -> None:
    app, _ = await _app(sqlite_db, "rtdup")

    with TestClient(app) as client:
        first = client.post(
            "/v1/connectors",
            json={"ref": "dup-ref", "backend_type": "pgvector", "params": PG_PARAMS},
            headers=operator_headers(),
        )
        second = client.post(
            "/v1/connectors",
            json={"ref": "dup-ref", "backend_type": "pgvector", "params": PG_PARAMS},
            headers=operator_headers(),
        )

    assert first.status_code == 201
    assert second.status_code == 409
    assert "PUT" in second.json()["detail"]


async def test_post_invalid_ref_and_bad_backend_rejected(sqlite_db) -> None:
    app, _ = await _app(sqlite_db, "invalid")

    with TestClient(app) as client:
        bad_ref = client.post(
            "/v1/connectors",
            json={"ref": "Not Valid!", "backend_type": "pgvector", "params": PG_PARAMS},
            headers=operator_headers(),
        )
        bad_backend = client.post(
            "/v1/connectors",
            json={"ref": "okay-ref", "backend_type": "mystery_db", "params": {}},
            headers=operator_headers(),
        )
        missing_param = client.post(
            "/v1/connectors",
            json={"ref": "no-dsn", "backend_type": "pgvector", "params": {}},
            headers=operator_headers(),
        )

    assert bad_ref.status_code == 422
    assert bad_backend.status_code == 422
    assert "unknown backend_type" in bad_backend.json()["detail"]
    assert missing_param.status_code == 422
    assert "dsn" in missing_param.json()["detail"]


async def test_put_unknown_ref_returns_404(sqlite_db) -> None:
    app, _ = await _app(sqlite_db, "put404")

    with TestClient(app) as client:
        r = client.put(
            "/v1/connectors/nope",
            json={"backend_type": "pgvector", "params": PG_PARAMS},
            headers=operator_headers(),
        )

    assert r.status_code == 404


async def test_put_rebuilds_and_persists(sqlite_db) -> None:
    app, service = await _app(sqlite_db, "put")

    with TestClient(app) as client:
        client.post(
            "/v1/connectors",
            json={"ref": "swappable", "backend_type": "pgvector", "params": PG_PARAMS},
            headers=operator_headers(),
        )
        r = client.put(
            "/v1/connectors/swappable",
            json={"backend_type": "redis_kv", "params": REDIS_PARAMS},
            headers=operator_headers(),
        )

    assert r.status_code == 200, r.text
    assert r.json()["backend_type"] == "redis_kv"
    manifest, _ = service.memory_registry.resolve("swappable")
    assert manifest.connector_type == "redis_kv"
    config = await service.memory_connector_config_repository.get("swappable")
    assert config.backend_type == "redis_kv"


async def test_delete_removes_runtime_connector(sqlite_db) -> None:
    app, service = await _app(sqlite_db, "delete")

    with TestClient(app) as client:
        client.post(
            "/v1/connectors",
            json={"ref": "doomed", "backend_type": "pgvector", "params": PG_PARAMS},
            headers=operator_headers(),
        )
        r = client.delete("/v1/connectors/doomed", headers=operator_headers())
        listing = client.get("/v1/connectors", headers=operator_headers())

    assert r.status_code == 204
    refs = {c["ref"] for c in listing.json()}
    assert "doomed" not in refs
    assert await service.memory_connector_config_repository.get("doomed") is None


async def test_delete_env_and_unknown_refs(sqlite_db) -> None:
    app, _ = await _app(sqlite_db, "delerr")

    with TestClient(app) as client:
        env = client.delete("/v1/connectors/key_value", headers=operator_headers())
        missing = client.delete("/v1/connectors/ghost", headers=operator_headers())

    assert env.status_code == 409
    assert missing.status_code == 404


async def test_persisted_configs_survive_bootstrap(sqlite_db) -> None:
    app, _ = await _app(sqlite_db, "persist")

    with TestClient(app) as client:
        r = client.post(
            "/v1/connectors",
            json={"ref": "durable", "backend_type": "pgvector", "params": PG_PARAMS},
            headers=operator_headers(),
        )
    assert r.status_code == 201

    # Simulate a fresh boot: new registry, repo over the same database.
    fresh_registry = InMemoryConnectorRegistry()
    repo = MemoryConnectorConfigRepository(sqlite_db)
    loaded = await load_persisted_connectors(fresh_registry, repo)

    assert "durable" in loaded
    manifest, connector = fresh_registry.resolve("durable")
    assert manifest.connector_type == "pgvector"
    assert type(connector).__name__ == "PgvectorMemoryConnector"


async def test_reviewer_cannot_mutate(sqlite_db) -> None:
    app, _ = await _app(sqlite_db, "rbac")

    with TestClient(app) as client:
        post = client.post(
            "/v1/connectors",
            json={"ref": "denied", "backend_type": "pgvector", "params": PG_PARAMS},
            headers=reviewer_headers(),
        )
        put = client.put(
            "/v1/connectors/denied",
            json={"backend_type": "pgvector", "params": PG_PARAMS},
            headers=reviewer_headers(),
        )
        delete = client.delete("/v1/connectors/denied", headers=reviewer_headers())
        probe = client.post("/v1/connectors/key_value/test", headers=reviewer_headers())

    assert post.status_code == 403
    assert put.status_code == 403
    assert delete.status_code == 403
    assert probe.status_code == 403


async def test_probe_env_connector_succeeds(sqlite_db) -> None:
    app, _ = await _app(sqlite_db, "probe")

    with TestClient(app) as client:
        r = client.post("/v1/connectors/key_value/test", headers=operator_headers())
        missing = client.post("/v1/connectors/ghost/test", headers=operator_headers())

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["detail"] is None
    assert body["latency_ms"] >= 0
    assert missing.status_code == 404


async def test_probe_is_tenant_namespaced_not_shared_cell(sqlite_db) -> None:
    """G9: the probe runs behind the tenant-scoping wrapper, so it must NOT touch
    the un-namespaced SHARED ``__shared__`` probe cell that two tenants sharing a
    backend would otherwise collide on.

    Seed the exact raw cell the pre-fix probe wrote/deleted, run the probe, and
    assert the seed survives — proving the probe targeted a tenant-namespaced
    cell instead. Before the fix, the probe's write+delete on the shared literal
    would have wiped the seed.
    """
    app, service = await _app(sqlite_db, "tenant-probe")
    _, raw = service.memory_registry.resolve("key_value")
    # The exact coordinates the raw probe used: SHARED scope, "__shared__" target.
    await raw.write("zeroth-connection-probe", "SEED", MemoryScope.SHARED, target="__shared__")

    with TestClient(app) as client:
        r = client.post("/v1/connectors/key_value/test", headers=operator_headers())

    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True  # happy path still works through the resolver
    # The un-namespaced shared cell is untouched: the probe used a tenant-scoped
    # target, so cross-tenant collision on one probe cell is impossible.
    survivor = await raw.read("zeroth-connection-probe", MemoryScope.SHARED, target="__shared__")
    assert survivor is not None and survivor.value == "SEED"


async def test_probe_unreachable_backend_returns_ok_false(sqlite_db) -> None:
    app, _ = await _app(sqlite_db, "probefail")

    with TestClient(app) as client:
        client.post(
            "/v1/connectors",
            json={
                "ref": "dead-redis",
                "backend_type": "redis_kv",
                "params": {"url": "redis://127.0.0.1:1/0"},
            },
            headers=operator_headers(),
        )
        r = client.post("/v1/connectors/dead-redis/test", headers=operator_headers())

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["detail"]
