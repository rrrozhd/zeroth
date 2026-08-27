"""Tests for runtime memory connector management (POST/PUT/DELETE/test)."""

from __future__ import annotations

import contextlib
from decimal import Decimal
from types import SimpleNamespace

from fastapi.testclient import TestClient

from tests.service.helpers import (
    agent_graph,
    deploy_service,
    operator_headers,
    reviewer_headers,
)
from zeroth.integrations.memory.config_repository import MemoryConnectorConfigRepository
from zeroth.integrations.memory.embedding_calls import invoke_embedding_call
from zeroth.integrations.memory.governed.models import MemoryScope
from zeroth.integrations.memory.models import ConnectorManifest
from zeroth.integrations.memory.registry import InMemoryConnectorRegistry
from zeroth.integrations.memory.runtime_configs import load_persisted_connectors
from zeroth.service.bootstrap import bootstrap_app

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
    class ProbeInstrumentation:
        def __init__(self) -> None:
            self.reserved = []
            self.released = []

        async def reserve_probe(self, **fields):
            self.reserved.append(fields)

        async def release_probe(self, **fields):
            self.released.append(fields)
            return SimpleNamespace(
                cost_event_id="cost-event-connector",
                cost_measurement="measured",
                provider_request_id=None,
                cleanup_status="complete",
            )

    app, service = await _app(sqlite_db, "probe")
    instrumentation = ProbeInstrumentation()
    service.probe_instrumentation = instrumentation
    service.orchestrator.per_run_cap_usd = 0.25

    with TestClient(app) as client:
        r = client.post(
            "/v1/connectors/key_value/test",
            headers=operator_headers(),
            json={
                "campaign_id": "campaign-1",
                "operation_id": "connector-check",
                "run_id": "run-1",
                "max_cost_usd": "0.02",
                "run_cap_usd": "0.25",
            },
        )
        missing = client.post("/v1/connectors/ghost/test", headers=operator_headers())

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["detail"] is None
    assert body["latency_ms"] >= 0
    assert body["campaign_id"] == "campaign-1"
    assert body["operation_id"] == "connector-check"
    assert body["cost_event_id"] == "cost-event-connector"
    assert body["cost_measurement"] == "measured"
    assert body["cleanup_status"] == "complete"
    assert instrumentation.reserved[0]["max_cost_usd"] == "0"
    assert instrumentation.released[0]["operation_id"] == "connector-check"
    assert missing.status_code == 404

    service.evaluation_campaign_id = "campaign-1"
    with TestClient(app) as client:
        uninstrumented = client.post(
            "/v1/connectors/key_value/test",
            headers=operator_headers(),
        )
    assert uninstrumented.status_code == 422


async def test_embedding_connector_probe_commits_usage_and_provider_identity(sqlite_db) -> None:
    class EmbeddingConnector:
        connector_type = "chroma"
        _embedding_model = "openai/text-embedding-3-small"

        async def write(self, key, value, scope, *, target=None):
            del key, value, scope, target
            await invoke_embedding_call(
                model=self._embedding_model,
                inputs=["probe"],
                provider_call=lambda: _embedding_response(),
            )

        async def read(self, key, scope, *, target=None):
            del key, scope, target
            return None

        async def delete(self, key, scope, *, target=None):
            del key, scope, target

    async def _embedding_response():
        return {
            "id": "embedding-request-probe",
            "usage": {"prompt_tokens": 5, "total_tokens": 5},
        }

    class Estimator:
        def estimate(self, model, *, input_tokens, output_tokens):
            del model, output_tokens
            return Decimal(str(input_tokens)) / Decimal("1000000")

    class ProbeInstrumentation:
        def __init__(self) -> None:
            self.reserved = []
            self.committed = []

        async def reserve_probe(self, **fields):
            self.reserved.append(fields)

        async def commit_probe(self, **fields):
            self.committed.append(fields)
            return SimpleNamespace(
                cost_event_id="cost-embedding-probe",
                cost_measurement="estimated",
                provider_request_id=fields["provider_request_id"],
                cleanup_status="complete",
            )

    app, service = await _app(sqlite_db, "embedding-probe")
    service.memory_registry.register(
        "embedding-probe",
        ConnectorManifest(connector_type="chroma", scope=MemoryScope.SHARED),
        EmbeddingConnector(),
    )
    instrumentation = ProbeInstrumentation()
    service.probe_instrumentation = instrumentation
    service.cost_estimator = Estimator()
    service.orchestrator.per_run_cap_usd = 0.25

    connector_schema = app.openapi()["components"]["schemas"]["ConnectorTestResponse"]
    assert {
        "campaign_id",
        "operation_id",
        "cost_event_id",
        "audit_event_id",
        "cost_measurement",
        "estimated_cost_usd",
        "provider_request_id",
        "cleanup_status",
    } <= set(connector_schema["properties"])

    with TestClient(app) as client:
        response = client.post(
            "/v1/connectors/embedding-probe/test",
            headers=operator_headers(),
            json={
                "campaign_id": "campaign-1",
                "operation_id": "connector-embedding-check",
                "run_id": "run-1",
                "max_cost_usd": "0.001",
                "run_cap_usd": "0.25",
            },
        )

    assert response.status_code == 200, response.text
    assert response.json()["cost_event_id"] == "cost-embedding-probe"
    assert response.json()["audit_event_id"] == "audit_cost-embedding-probe"
    assert response.json()["estimated_cost_usd"] == "0.000005"
    assert response.json()["provider_request_id"] == "embedding-request-probe"
    assert response.json()["cleanup_status"] == "complete"
    assert len(instrumentation.reserved) == 1
    assert len(instrumentation.committed) == 1
    assert instrumentation.reserved[0]["implementation_id"] == ("openai/text-embedding-3-small")
    assert (
        instrumentation.reserved[0]["implementation_id"]
        == (instrumentation.committed[0]["implementation_id"])
    )
    assert instrumentation.committed[0]["operation_id"] == "connector-embedding-check"
    assert instrumentation.committed[0]["actual_cost_usd"] == "0.000005"


async def test_probe_is_tenant_namespaced_not_shared_cell(sqlite_db) -> None:
    """G9: the probe runs behind the tenant-scoping wrapper, so it must NOT touch.

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
    # A02-8: the probe reports WHY it failed, from a closed set -- not the
    # driver's own text, which names the host and port it dialled. Asserted over
    # the whole serialized body, and at 200: this route deliberately never 500s,
    # so a check scoped to error status codes would not see the leak.
    assert body["detail"]
    for fragment in ("127.0.0.1", ":1/0", "redis://"):
        assert fragment not in r.text, f"{fragment!r} leaked into the probe body"


@contextlib.contextmanager
def _refuse(repository, method: str):
    async def refuse(*args, **kwargs):
        raise RuntimeError(f"injected {method} failure")

    original = getattr(repository, method)
    setattr(repository, method, refuse)
    try:
        yield
    finally:
        setattr(repository, method, original)


async def test_create_does_not_go_live_when_the_config_cannot_be_persisted(sqlite_db) -> None:
    """A02-19: nothing serves traffic that no durable record backs.

    Create used to register the connector into the live in-process registry
    first and persist it second, so a failed write left a connector answering
    ``connector_ref`` lookups in this process and nowhere else -- it vanished at
    the next restart, and the operator got a 500 saying it was never created.
    """
    app, service = await _app(sqlite_db, "create-persist-fails")

    with _refuse(service.memory_connector_config_repository, "upsert"):
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post(
                "/v1/connectors",
                json={"ref": "ghost", "backend_type": "pgvector", "params": PG_PARAMS},
                headers=operator_headers(),
            )

    assert response.status_code >= 500
    assert "ghost" not in service.memory_registry.list(), (
        "an unpersisted connector is live in this process and vanishes on restart"
    )


async def test_delete_keeps_the_connector_live_when_the_row_cannot_be_deleted(sqlite_db) -> None:
    """A02-19: a delete that does not reach the database changes nothing.

    Delete used to unregister from the live registry first, so a failed row
    delete left the connector unresolvable in this process while the config
    survived -- and the next restart resurrected it.
    """
    app, service = await _app(sqlite_db, "delete-persist-fails")

    with TestClient(app) as client:
        created = client.post(
            "/v1/connectors",
            json={"ref": "doomed", "backend_type": "pgvector", "params": PG_PARAMS},
            headers=operator_headers(),
        )
        assert created.status_code == 201, created.text

    with _refuse(service.memory_connector_config_repository, "delete"):
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.delete("/v1/connectors/doomed", headers=operator_headers())

    assert response.status_code >= 500
    assert "doomed" in service.memory_registry.list(), (
        "the connector is gone from this process but its config survives a restart"
    )


async def test_update_keeps_the_previous_backend_live_when_the_write_fails(sqlite_db) -> None:
    """A02-19: a reconfiguration that is not persisted must not take effect."""
    app, service = await _app(sqlite_db, "update-persist-fails")

    with TestClient(app) as client:
        created = client.post(
            "/v1/connectors",
            json={"ref": "swappable", "backend_type": "pgvector", "params": PG_PARAMS},
            headers=operator_headers(),
        )
        assert created.status_code == 201, created.text

    with _refuse(service.memory_connector_config_repository, "upsert"):
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.put(
                "/v1/connectors/swappable",
                json={"backend_type": "redis_kv", "params": REDIS_PARAMS},
                headers=operator_headers(),
            )

    assert response.status_code >= 500
    manifest, _ = service.memory_registry.resolve("swappable")
    assert manifest.connector_type == "pgvector", (
        "an unpersisted reconfiguration is live and reverts on restart"
    )
