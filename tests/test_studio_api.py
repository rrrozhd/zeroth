"""Tests for Studio graph authoring REST API."""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from zeroth.contracts.graph.repository import GraphRepository
from zeroth.governance.identity import AuthenticatedPrincipal, AuthMethod, ServiceRole
from zeroth.platform.storage.async_sqlite import AsyncSQLiteDatabase
from zeroth.service.api.studio_api import router as studio_router
from zeroth.service.bootstrap.migrations import run_migrations


def _make_app(
    graph_repo: GraphRepository | None = None,
    *,
    roles: list[ServiceRole] | None = None,
) -> FastAPI:
    """Create a minimal FastAPI app with Studio routes.

    Injects an authenticated principal the way the production auth middleware
    would, so route-level RBAC (require_permission) is exercised. Defaults to an
    ADMIN principal so behavioral tests see the routes' happy path; pass ``roles``
    to assert authorization boundaries.
    """
    app = FastAPI()
    bootstrap = MagicMock()
    if graph_repo is not None:
        bootstrap.graph_repository = graph_repo
    else:
        bootstrap.graph_repository = MagicMock(spec=GraphRepository)
    # Denial auditing no-ops without a repository; keep it None so authz-failure
    # tests don't try to serialize MagicMock attributes into a NodeAuditRecord.
    bootstrap.audit_repository = None
    bootstrap.contract_registry = None
    bootstrap.memory_registry = None
    bootstrap.orchestrator = None
    bootstrap.provider_verification_adapter = None
    bootstrap.deployment_service = None
    app.state.bootstrap = bootstrap

    principal = AuthenticatedPrincipal(
        subject="test",
        auth_method=AuthMethod.API_KEY,
        roles=roles if roles is not None else [ServiceRole.ADMIN],
    )

    @app.middleware("http")
    async def _inject_principal(request, call_next):
        request.state.principal = principal
        return await call_next(request)

    app.include_router(studio_router)
    return app


def _make_repo(tmp_path: Path | None = None) -> GraphRepository:
    """Create a real GraphRepository backed by an async SQLite database."""
    if tmp_path is None:
        tmp_path = Path(tempfile.mkdtemp())
    db_path = tmp_path / "test_studio.db"
    run_migrations(f"sqlite:///{db_path}")
    db = AsyncSQLiteDatabase(str(db_path))
    return GraphRepository(db)


class TestCreateWorkflow:
    """POST /api/studio/v1/workflows."""

    def test_create_workflow(self) -> None:
        repo = _make_repo()
        app = _make_app(repo)
        client = TestClient(app)

        resp = client.post("/api/studio/v1/workflows", json={"name": "test"})

        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "test"
        assert "id" in data
        assert data["version"] == 1
        assert data["status"] == "draft"
        assert data["entry_step"] is None
        assert data["nodes"] == []
        assert data["edges"] == []
        assert "viewport" in data
        assert data["execution_settings"] == {
            "max_total_steps": 1000,
            "max_total_runtime_seconds": None,
            "max_visits_per_node": 10,
            "max_visits_per_edge": None,
            "default_timeout_seconds": None,
        }

    def test_create_workflow_empty_name_rejected(self) -> None:
        repo = _make_repo()
        app = _make_app(repo)
        client = TestClient(app)

        resp = client.post("/api/studio/v1/workflows", json={"name": ""})
        assert resp.status_code == 422


class TestListWorkflows:
    """GET /api/studio/v1/workflows."""

    def test_list_workflows(self) -> None:
        repo = _make_repo()
        app = _make_app(repo)
        client = TestClient(app)

        # Create two workflows
        client.post("/api/studio/v1/workflows", json={"name": "wf1"})
        client.post("/api/studio/v1/workflows", json={"name": "wf2"})

        resp = client.get("/api/studio/v1/workflows")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        names = {item["name"] for item in data}
        assert names == {"wf1", "wf2"}
        # Summary should have id, name, version, status, updated_at
        for item in data:
            assert "id" in item
            assert "name" in item
            assert "version" in item
            assert "status" in item
            assert "updated_at" in item


class TestGetWorkflow:
    """GET /api/studio/v1/workflows/{workflow_id}."""

    def test_get_workflow(self) -> None:
        repo = _make_repo()
        app = _make_app(repo)
        client = TestClient(app)

        create_resp = client.post("/api/studio/v1/workflows", json={"name": "detail-test"})
        wf_id = create_resp.json()["id"]

        resp = client.get(f"/api/studio/v1/workflows/{wf_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == wf_id
        assert data["name"] == "detail-test"
        assert "nodes" in data
        assert "edges" in data
        assert "viewport" in data

    def test_get_workflow_not_found(self) -> None:
        repo = _make_repo()
        app = _make_app(repo)
        client = TestClient(app)

        resp = client.get("/api/studio/v1/workflows/nonexistent")
        assert resp.status_code == 404


class TestUpdateWorkflow:
    """PUT /api/studio/v1/workflows/{workflow_id}."""

    def test_update_workflow_name(self) -> None:
        repo = _make_repo()
        app = _make_app(repo)
        client = TestClient(app)

        create_resp = client.post("/api/studio/v1/workflows", json={"name": "original"})
        wf_id = create_resp.json()["id"]

        resp = client.put(f"/api/studio/v1/workflows/{wf_id}", json={"name": "updated"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "updated"

    def test_update_loop_safety_settings_round_trips_and_preserves_omitted_values(self) -> None:
        repo = _make_repo()
        app = _make_app(repo)
        client = TestClient(app)

        create_resp = client.post("/api/studio/v1/workflows", json={"name": "bounded-loop"})
        wf_id = create_resp.json()["id"]

        updated = client.put(
            f"/api/studio/v1/workflows/{wf_id}",
            json={
                "execution_settings": {
                    "max_total_steps": 24,
                    "max_total_runtime_seconds": 45,
                    "max_visits_per_node": 4,
                    "max_visits_per_edge": 3,
                }
            },
        )

        assert updated.status_code == 200
        assert updated.json()["execution_settings"] == {
            "max_total_steps": 24,
            "max_total_runtime_seconds": 45,
            "max_visits_per_node": 4,
            "max_visits_per_edge": 3,
            "default_timeout_seconds": None,
        }

        renamed = client.put(f"/api/studio/v1/workflows/{wf_id}", json={"name": "renamed"})
        assert renamed.status_code == 200
        assert renamed.json()["execution_settings"]["max_visits_per_edge"] == 3

    def test_update_loop_safety_settings_rejects_unbounded_values(self) -> None:
        repo = _make_repo()
        app = _make_app(repo)
        client = TestClient(app)

        create_resp = client.post("/api/studio/v1/workflows", json={"name": "bounded-loop"})
        wf_id = create_resp.json()["id"]

        response = client.put(
            f"/api/studio/v1/workflows/{wf_id}",
            json={"execution_settings": {"max_total_steps": 0}},
        )

        assert response.status_code == 422

    def test_update_workflow_not_found(self) -> None:
        repo = _make_repo()
        app = _make_app(repo)
        client = TestClient(app)

        resp = client.put("/api/studio/v1/workflows/nonexistent", json={"name": "x"})
        assert resp.status_code == 404


class TestDeleteWorkflow:
    """DELETE /api/studio/v1/workflows/{workflow_id}."""

    def test_delete_workflow(self) -> None:
        repo = _make_repo()
        app = _make_app(repo)
        client = TestClient(app)

        create_resp = client.post("/api/studio/v1/workflows", json={"name": "to-delete"})
        wf_id = create_resp.json()["id"]

        resp = client.delete(f"/api/studio/v1/workflows/{wf_id}")
        assert resp.status_code == 204

    def test_delete_workflow_not_found(self) -> None:
        repo = _make_repo()
        app = _make_app(repo)
        client = TestClient(app)

        resp = client.delete("/api/studio/v1/workflows/nonexistent")
        assert resp.status_code == 404


class TestListNodeTypes:
    """GET /api/studio/v1/node-types."""

    def test_list_node_types(self) -> None:
        repo = _make_repo()
        app = _make_app(repo)
        client = TestClient(app)

        resp = client.get("/api/studio/v1/node-types")
        assert resp.status_code == 200
        data = resp.json()
        # The registry mirrors the executable graph model's node_type
        # discriminator, plus "code" — the canvas alias for an executable unit
        # whose source is authored inline. It is no longer the same list as the
        # *palette*: "mcp_tool" is registered so the canvas can resolve its
        # ports (a node with none would draw without handles and its tool edge
        # would silently fail to attach) but it is served in the "imported"
        # category, which the palette filters out, because an imported tool is
        # pinned to a schema digest canvas authoring cannot produce.
        #
        # The category carries this rather than a dedicated flag: NodeTypeResponse
        # is an immutable legacy capability pinned in backend_surface_legacy.json,
        # so it may not gain a field.
        type_names = {item["type"] for item in data}
        assert type_names == {
            "agent",
            "code",
            "entrypoint",
            "executable_unit",
            "human_approval",
            "http_request",
            "if",
            "loop",
            "mcp_tool",
            "retrieval",
            "subgraph",
        }
        palette = {item["type"] for item in data if item["category"] != "imported"}
        assert "mcp_tool" not in palette
        assert palette == type_names - {"mcp_tool"}
        assert {item["category"] for item in data if item["type"] == "mcp_tool"} == {"imported"}
        # Each should have type, label, category, ports
        for item in data:
            assert "type" in item
            assert "label" in item
            assert "category" in item
            assert "ports" in item
            assert len(item["ports"]) > 0
            for port in item["ports"]:
                assert "id" in port
                assert "type" in port
                assert "direction" in port
                assert "label" in port

    def test_standalone_magicmock_bootstrap_does_not_fabricate_a_deployment_scope(self) -> None:
        app = _make_app()

        with TestClient(app) as client:
            response = client.get("/api/studio/v1/node-types")

        assert response.status_code == 200
        assert "deployment" not in vars(app.state.bootstrap).get("_mock_children", {})


class TestStructuralAuthoring:
    """PUT persists real executable nodes/edges (not just visual metadata)."""

    def test_http_request_node_round_trips_with_required_capabilities(self) -> None:
        repo = _make_repo()
        client = TestClient(_make_app(repo))
        wf_id = client.post("/api/studio/v1/workflows", json={"name": "http"}).json()["id"]

        response = client.put(
            f"/api/studio/v1/workflows/{wf_id}",
            json={
                "nodes": [
                    {
                        "id": "fetch",
                        "type": "http_request",
                        "position": {"x": 100, "y": 40},
                        "data": {
                            "label": "Fetch local health",
                            "config": {
                                "url": "http://127.0.0.1:8787/health",
                                "timeout_seconds": 2,
                                "max_retries": 1,
                                "retryable_status_codes": [429, 503],
                                "max_response_bytes": 4096,
                            },
                        },
                    }
                ],
                "edges": [],
            },
        )

        assert response.status_code == 200, response.text
        node = response.json()["nodes"][0]
        assert node["type"] == "http_request"
        assert node["data"]["capability_bindings"] == [
            "network_read",
            "external_api_call",
        ]
        assert node["data"]["config"]["method"] == "GET"

    def test_http_request_node_rejects_public_host_at_save_boundary(self) -> None:
        repo = _make_repo()
        client = TestClient(_make_app(repo))
        wf_id = client.post("/api/studio/v1/workflows", json={"name": "http"}).json()["id"]

        response = client.put(
            f"/api/studio/v1/workflows/{wf_id}",
            json={
                "nodes": [
                    {
                        "id": "fetch",
                        "type": "http_request",
                        "position": {"x": 0, "y": 0},
                        "data": {
                            "label": "Unsafe fetch",
                            "config": {"url": "https://example.com/data"},
                        },
                    }
                ],
                "edges": [],
            },
        )

        assert response.status_code == 422
        assert "literal private IP" in response.json()["detail"]

    def test_persist_nodes_and_edges_round_trip(self) -> None:
        repo = _make_repo()
        app = _make_app(repo)
        client = TestClient(app)

        wf_id = client.post("/api/studio/v1/workflows", json={"name": "authored"}).json()["id"]

        body = {
            "nodes": [
                {
                    "id": "gate",
                    "type": "human_approval",
                    "position": {"x": 10, "y": 20},
                    "data": {"label": "Review gate", "config": {}},
                },
                {
                    "id": "writer",
                    "type": "agent",
                    "position": {"x": 200, "y": 20},
                    "data": {
                        "label": "Writer",
                        "config": {
                            "instruction": "Write a summary",
                            "model_provider": "openai/gpt-4o",
                        },
                    },
                },
            ],
            "edges": [
                {
                    "id": "e1",
                    "source": "gate",
                    "target": "writer",
                    "source_handle": "output-data",
                    "target_handle": "input-data",
                }
            ],
        }
        resp = client.put(f"/api/studio/v1/workflows/{wf_id}", json=body)
        assert resp.status_code == 200, resp.text

        # Re-fetch: nodes/edges/config must round-trip.
        detail = client.get(f"/api/studio/v1/workflows/{wf_id}").json()
        nodes = {n["id"]: n for n in detail["nodes"]}
        assert set(nodes) == {"gate", "writer"}
        assert nodes["writer"]["type"] == "agent"
        assert nodes["writer"]["data"]["config"]["instruction"] == "Write a summary"
        assert nodes["gate"]["position"] == {"x": 10, "y": 20}
        assert len(detail["edges"]) == 1
        assert detail["edges"][0]["source"] == "gate"
        assert detail["edges"][0]["source_handle"] == "output-data"

    def test_invalid_node_config_rejected(self) -> None:
        repo = _make_repo()
        app = _make_app(repo)
        client = TestClient(app)
        wf_id = client.post("/api/studio/v1/workflows", json={"name": "bad"}).json()["id"]

        # agent requires instruction + model_provider
        resp = client.put(
            f"/api/studio/v1/workflows/{wf_id}",
            json={
                "nodes": [
                    {
                        "id": "a",
                        "type": "agent",
                        "position": {"x": 0, "y": 0},
                        "data": {"config": {}},
                    }
                ],
                "edges": [],
            },
        )
        assert resp.status_code == 422

    def test_loop_node_and_named_ports_round_trip(self) -> None:
        repo = _make_repo()
        client = TestClient(_make_app(repo))
        wf_id = client.post("/api/studio/v1/workflows", json={"name": "loop"}).json()["id"]

        response = client.put(
            f"/api/studio/v1/workflows/{wf_id}",
            json={
                "nodes": [
                    {
                        "id": "quality-loop",
                        "type": "loop",
                        "position": {"x": 100, "y": 40},
                        "data": {
                            "label": "Quality loop",
                            "config": {
                                "until": "payload.needs_repair != True",
                                "max_retries": 3,
                            },
                        },
                    }
                ],
                "edges": [],
            },
        )

        assert response.status_code == 200, response.text
        detail = client.get(f"/api/studio/v1/workflows/{wf_id}").json()
        assert detail["nodes"][0]["type"] == "loop"
        assert detail["nodes"][0]["data"]["config"] == {
            "until": "payload.needs_repair != True",
            "max_retries": 3,
        }
        loop_type = next(
            item for item in client.get("/api/studio/v1/node-types").json()
            if item["type"] == "loop"
        )
        assert [port["id"] for port in loop_type["ports"]] == [
            "input-data",
            "repeat",
            "done",
            "limit",
        ]

    def test_if_node_and_named_ports_round_trip(self) -> None:
        repo = _make_repo()
        client = TestClient(_make_app(repo))
        wf_id = client.post("/api/studio/v1/workflows", json={"name": "decision"}).json()["id"]

        response = client.put(
            f"/api/studio/v1/workflows/{wf_id}",
            json={
                "nodes": [
                    {
                        "id": "quality-gate",
                        "type": "if",
                        "position": {"x": 100, "y": 40},
                        "data": {
                            "label": "Quality gate",
                            "config": {"expression": "payload.score >= 0.8"},
                        },
                    }
                ],
                "edges": [],
            },
        )

        assert response.status_code == 200, response.text
        detail = client.get(f"/api/studio/v1/workflows/{wf_id}").json()
        assert detail["nodes"][0]["type"] == "if"
        assert detail["nodes"][0]["data"]["config"] == {
            "expression": "payload.score >= 0.8"
        }
        if_type = next(
            item for item in client.get("/api/studio/v1/node-types").json()
            if item["type"] == "if"
        )
        assert [port["id"] for port in if_type["ports"]] == [
            "input-data",
            "true",
            "false",
        ]

    def test_if_draft_can_be_incomplete_and_server_normalizes_named_routes(self) -> None:
        client = TestClient(_make_app(_make_repo()))
        wf_id = client.post("/api/studio/v1/workflows", json={"name": "decision"}).json()["id"]

        response = client.put(
            f"/api/studio/v1/workflows/{wf_id}",
            json={
                "nodes": [
                    {
                        "id": "quality-gate",
                        "type": "if",
                        "position": {"x": 100, "y": 40},
                        "data": {"label": "Quality gate", "config": {"expression": ""}},
                    },
                    {
                        "id": "accepted",
                        "type": "entrypoint",
                        "position": {"x": 380, "y": 40},
                        "data": {"label": "Accepted", "config": {}},
                    },
                ],
                "edges": [
                    {
                        "id": "true-route",
                        "source": "quality-gate",
                        "target": "accepted",
                        "source_handle": "true",
                        "condition": {
                            "expression": "payload.forged == True",
                            "metadata": {"if_route": "false"},
                        },
                    }
                ],
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["nodes"][0]["data"]["config"] == {"expression": ""}
        assert response.json()["edges"][0]["condition"] == {
            "expression": "payload.zeroth_if['quality-gate'].route == 'true'",
            "operand_refs": [],
            "branch_rule": "expression",
            "allow_cycle_traversal": False,
            "metadata": {"if_route": "true"},
        }

    def test_edge_to_unknown_node_rejected(self) -> None:
        repo = _make_repo()
        app = _make_app(repo)
        client = TestClient(app)
        wf_id = client.post("/api/studio/v1/workflows", json={"name": "dangling"}).json()["id"]
        resp = client.put(
            f"/api/studio/v1/workflows/{wf_id}",
            json={
                "nodes": [],
                "edges": [{"id": "e", "source": "ghost", "target": "ghost2"}],
            },
        )
        assert resp.status_code == 422


class TestGovernanceFieldPreservation:
    """Canvas saves must not wipe NodeBase governance fields (v0.9 regression).

    Bindings authored via the API/Python were silently dropped by any Studio
    structural save: the serializer never sent them and the rebuild reset them
    to defaults — under enforce-by-default that stripped a graph's capability
    restrictions. Semantics now: key present in data wins (explicit [] clears),
    absent key preserves the stored node's value.
    """

    _AGENT_CONFIG = {"instruction": "Summarize", "model_provider": "openai/gpt-4o"}
    _GOVERNED_DATA = {
        "label": "Writer",
        "config": _AGENT_CONFIG,
        "capability_bindings": ["MEMORY_READ"],
        "policy_bindings": ["policy-pii"],
        "execution_config": {"timeout_seconds": 30},
        "audit_config": {"level": "full"},
        "parallel_config": {"split_path": "items"},
    }

    @staticmethod
    def _put_nodes(client: TestClient, wf_id: str, data: dict) -> None:
        resp = client.put(
            f"/api/studio/v1/workflows/{wf_id}",
            json={
                "nodes": [
                    {"id": "writer", "type": "agent", "position": {"x": 0, "y": 0}, "data": data}
                ],
                "edges": [],
            },
        )
        assert resp.status_code == 200, resp.text

    @staticmethod
    def _node_data(client: TestClient, wf_id: str) -> dict:
        detail = client.get(f"/api/studio/v1/workflows/{wf_id}").json()
        return {n["id"]: n for n in detail["nodes"]}["writer"]["data"]

    def test_get_exposes_governance_fields_with_defaults(self) -> None:
        repo = _make_repo()
        client = TestClient(_make_app(repo))
        wf_id = client.post("/api/studio/v1/workflows", json={"name": "gov"}).json()["id"]

        self._put_nodes(client, wf_id, {"label": "Writer", "config": self._AGENT_CONFIG})

        data = self._node_data(client, wf_id)
        assert data["capability_bindings"] == []
        assert data["policy_bindings"] == []
        assert data["execution_config"] == {}
        assert data["audit_config"] == {}
        assert data["parallel_config"] is None

    def test_canvas_save_preserves_governance_fields(self) -> None:
        repo = _make_repo()
        client = TestClient(_make_app(repo))
        wf_id = client.post("/api/studio/v1/workflows", json={"name": "gov"}).json()["id"]

        # Author governance fields (as the API/Python would).
        self._put_nodes(client, wf_id, dict(self._GOVERNED_DATA))

        # A canvas-shaped save: only the fields the console knows about.
        self._put_nodes(client, wf_id, {"label": "Writer 2", "config": self._AGENT_CONFIG})

        data = self._node_data(client, wf_id)
        assert data["label"] == "Writer 2"
        assert data["capability_bindings"] == ["MEMORY_READ"]
        assert data["policy_bindings"] == ["policy-pii"]
        assert data["execution_config"] == {"timeout_seconds": 30}
        assert data["audit_config"] == {"level": "full"}
        assert data["parallel_config"]["split_path"] == "items"

    def test_explicit_empty_clears_bindings(self) -> None:
        repo = _make_repo()
        client = TestClient(_make_app(repo))
        wf_id = client.post("/api/studio/v1/workflows", json={"name": "gov"}).json()["id"]

        self._put_nodes(client, wf_id, dict(self._GOVERNED_DATA))
        self._put_nodes(
            client,
            wf_id,
            {
                "label": "Writer",
                "config": self._AGENT_CONFIG,
                "capability_bindings": [],
                "policy_bindings": [],
                "parallel_config": None,
            },
        )

        data = self._node_data(client, wf_id)
        assert data["capability_bindings"] == []
        assert data["policy_bindings"] == []
        assert data["parallel_config"] is None
        # Keys absent from that save are still preserved, not cleared.
        assert data["execution_config"] == {"timeout_seconds": 30}
        assert data["audit_config"] == {"level": "full"}


class TestAdvancedExecutionRoundTrip:
    """Studio must author and preserve the runtime's advanced graph semantics."""

    _AGENT_CONFIG = {"instruction": "Process", "model_provider": "openai/gpt-4o"}

    @staticmethod
    def _nodes() -> list[dict]:
        return [
            {
                "id": "source",
                "type": "agent",
                "position": {"x": 0, "y": 0},
                "data": {
                    "label": "Source",
                    "config": TestAdvancedExecutionRoundTrip._AGENT_CONFIG,
                    "parallel_config": {
                        "split_path": "items",
                        "merge_strategy": "collect",
                        "fail_mode": "best_effort",
                        "max_branches": 12,
                        "max_concurrency": 3,
                        "batch_size": 2,
                        "branch_timeout_seconds": 5,
                    },
                },
            },
            {
                "id": "join",
                "type": "agent",
                "position": {"x": 200, "y": 0},
                "data": {
                    "label": "Join",
                    "config": TestAdvancedExecutionRoundTrip._AGENT_CONFIG,
                    "join_config": {
                        "merge_strategy": "collect",
                        "merge_path": "results",
                    },
                },
            },
        ]

    @staticmethod
    def _advanced_edge() -> dict:
        return {
            "id": "loop",
            "source": "source",
            "target": "join",
            "kind": "data",
            "condition": {
                "expression": "payload.iteration < 3",
                "operand_refs": ["payload.iteration"],
                "branch_rule": "expression",
                "allow_cycle_traversal": True,
                "metadata": {"purpose": "bounded-loop"},
            },
            "mapping": {
                "operations": [
                    {
                        "operation": "rename",
                        "source_path": "payload.items",
                        "target_path": "items",
                    }
                ]
            },
            "enabled": False,
        }

    def test_advanced_node_and_edge_fields_round_trip(self) -> None:
        client = TestClient(_make_app(_make_repo()))
        workflow_id = client.post(
            "/api/studio/v1/workflows", json={"name": "advanced"}
        ).json()["id"]

        response = client.put(
            f"/api/studio/v1/workflows/{workflow_id}",
            json={"nodes": self._nodes(), "edges": [self._advanced_edge()]},
        )

        assert response.status_code == 200, response.text
        detail = response.json()
        nodes = {node["id"]: node["data"] for node in detail["nodes"]}
        assert nodes["source"]["parallel_config"] == {
            "split_path": "items",
            "merge_strategy": "collect",
            "reducer_ref": None,
            "fail_mode": "best_effort",
            "max_branches": 12,
            "max_concurrency": 3,
            "batch_size": 2,
            "branch_timeout_seconds": 5.0,
        }
        assert nodes["join"]["join_config"] == {
            "merge_strategy": "collect",
            "reducer_ref": None,
            "merge_path": "results",
        }
        edge = detail["edges"][0]
        assert edge["condition"] == self._advanced_edge()["condition"]
        assert edge["mapping"] == self._advanced_edge()["mapping"]
        assert edge["enabled"] is False

    def test_legacy_shaped_save_preserves_advanced_edge_fields(self) -> None:
        client = TestClient(_make_app(_make_repo()))
        workflow_id = client.post(
            "/api/studio/v1/workflows", json={"name": "advanced"}
        ).json()["id"]
        nodes = self._nodes()
        advanced_edge = self._advanced_edge()
        first = client.put(
            f"/api/studio/v1/workflows/{workflow_id}",
            json={"nodes": nodes, "edges": [advanced_edge]},
        )
        assert first.status_code == 200, first.text

        legacy_edge = {
            key: value
            for key, value in advanced_edge.items()
            if key not in {"condition", "mapping", "enabled"}
        }
        second = client.put(
            f"/api/studio/v1/workflows/{workflow_id}",
            json={"nodes": nodes, "edges": [legacy_edge]},
        )

        assert second.status_code == 200, second.text
        edge = second.json()["edges"][0]
        assert edge["condition"] == advanced_edge["condition"]
        assert edge["mapping"] == advanced_edge["mapping"]
        assert edge["enabled"] is False


class TestWorkflowPreflight:
    def test_http_request_draft_requires_a_url_before_publish(self) -> None:
        repo = _make_repo()
        client = TestClient(_make_app(repo))
        workflow_id = client.post(
            "/api/studio/v1/workflows", json={"name": "http-draft"}
        ).json()["id"]
        saved = client.put(
            f"/api/studio/v1/workflows/{workflow_id}",
            json={
                "entry_step": "fetch",
                "nodes": [
                    {
                        "id": "fetch",
                        "type": "http_request",
                        "position": {"x": 0, "y": 0},
                        "data": {"label": "Fetch", "config": {"url": ""}},
                    }
                ],
                "edges": [],
            },
        )
        assert saved.status_code == 200, saved.text

        preflight = client.post(f"/api/studio/v1/workflows/{workflow_id}/preflight")
        assert preflight.status_code == 200
        assert preflight.json()["ready"] is False
        assert any(
            issue["code"] == "missing_http_request_url"
            for issue in preflight.json()["issues"]
        )

        publish = client.post(f"/api/studio/v1/workflows/{workflow_id}/publish")
        assert publish.status_code == 422

    def test_subgraph_preflight_uses_runtime_deployment_reference(self) -> None:
        repo = _make_repo()
        app = _make_app(repo)
        app.state.bootstrap.deployment_service = SimpleNamespace(
            list=AsyncMock(
                return_value=[SimpleNamespace(deployment_ref="template-research")]
            )
        )
        client = TestClient(app)
        workflow_id = client.post(
            "/api/studio/v1/workflows", json={"name": "subgraph-parent"}
        ).json()["id"]
        saved = client.put(
            f"/api/studio/v1/workflows/{workflow_id}",
            json={
                "entry_step": "start",
                "nodes": [
                    {
                        "id": "start",
                        "type": "entrypoint",
                        "position": {"x": 0, "y": 0},
                        "data": {"label": "Start", "config": {}},
                    },
                    {
                        "id": "research",
                        "type": "subgraph",
                        "position": {"x": 200, "y": 0},
                        "data": {
                            "label": "Research",
                            "config": {
                                "graph_ref": "template-research",
                                "thread_participation": "isolated",
                                "max_depth": 2,
                            },
                        },
                    },
                ],
                "edges": [
                    {
                        "id": "invoke",
                        "source": "start",
                        "target": "research",
                        "kind": "data",
                    }
                ],
            },
        )
        assert saved.status_code == 200, saved.text

        response = client.post(f"/api/studio/v1/workflows/{workflow_id}/preflight")

        assert response.status_code == 200, response.text
        assert not any(
            issue["code"] == "unresolved_subgraph_ref"
            for issue in response.json()["issues"]
        )
        app.state.bootstrap.deployment_service.list.assert_awaited_once()

    def test_reports_unresolved_runtime_dependencies_without_executing_them(self) -> None:
        repo = _make_repo()
        app = _make_app(repo)
        memory_registry = MagicMock()
        memory_registry.list.return_value = {}
        app.state.bootstrap.memory_registry = memory_registry
        client = TestClient(app)
        workflow_id = client.post(
            "/api/studio/v1/workflows", json={"name": "preflight"}
        ).json()["id"]
        nodes = [
            {
                "id": "start",
                "type": "entrypoint",
                "position": {"x": 0, "y": 0},
                "data": {"label": "Start", "config": {}},
            },
            {
                "id": "search",
                "type": "retrieval",
                "position": {"x": 200, "y": 0},
                "data": {
                    "label": "Search",
                    "config": {"connector_ref": "missing-search", "top_k": 3},
                },
            },
        ]
        edge = {
            "id": "search-edge",
            "source": "start",
            "target": "search",
            "kind": "data",
        }
        saved = client.put(
            f"/api/studio/v1/workflows/{workflow_id}",
            json={"entry_step": "start", "nodes": nodes, "edges": [edge]},
        )
        assert saved.status_code == 200, saved.text

        response = client.post(f"/api/studio/v1/workflows/{workflow_id}/preflight")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ready"] is False
        assert "connectors" in body["checks"]
        assert any(
            issue["code"] == "unresolved_connector_ref" and issue["node_id"] == "search"
            for issue in body["issues"]
        )
        memory_registry.list.assert_called_once_with()

        publish = client.post(f"/api/studio/v1/workflows/{workflow_id}/publish")
        assert publish.status_code == 422
        assert publish.json()["detail"]["message"] == "workflow failed mandatory preflight"
        assert any(
            issue["code"] == "unresolved_connector_ref"
            for issue in publish.json()["detail"]["issues"]
        )

    def test_live_provider_verification_requires_consent_and_is_bounded(self) -> None:
        from zeroth.governance.audit.models import TokenUsage
        from zeroth.runtime.agents.provider import DeterministicProviderAdapter, ProviderResponse

        class ProbeInstrumentation:
            def __init__(self) -> None:
                self.reserved = []
                self.committed = []
                self.ambiguous = []

            async def reserve_probe(self, **fields):
                self.reserved.append(fields)

            async def commit_probe(self, **fields):
                self.committed.append(fields)
                return SimpleNamespace(
                    cost_event_id="cost-event-provider",
                    cost_measurement="estimated",
                    provider_request_id="provider-request-1",
                    cleanup_status="complete",
                )

            async def mark_probe_ambiguous(self, **fields):
                self.ambiguous.append(fields)
                return SimpleNamespace(
                    cost_event_id="cost-event-ambiguous",
                    cost_measurement="unmeasured",
                    provider_request_id=None,
                    cleanup_status="pending_reconciliation",
                )

        repo = _make_repo()
        app = _make_app(repo)
        adapter = DeterministicProviderAdapter(
            [
                ProviderResponse(
                    content="OK",
                    token_usage=TokenUsage(
                        input_tokens=11,
                        output_tokens=2,
                        total_tokens=13,
                        model_name="openai/gpt-4o-mini",
                    ),
                )
            ]
        )
        instrumentation = ProbeInstrumentation()
        app.state.bootstrap.provider_verification_adapter = adapter
        app.state.bootstrap.probe_instrumentation = instrumentation
        app.state.bootstrap.cost_estimator = SimpleNamespace(
            estimate=lambda *args, **kwargs: __import__("decimal").Decimal("0.01")
        )
        app.state.bootstrap.orchestrator = SimpleNamespace(per_run_cap_usd=0.25)
        provider_schema = app.openapi()["components"]["schemas"]["LiveProviderProbe"]
        assert {
            "operation_id",
            "cost_event_id",
            "audit_event_id",
            "cost_measurement",
            "estimated_cost_usd",
            "provider_request_id",
            "cleanup_status",
        } <= set(provider_schema["properties"])
        client = TestClient(app)
        workflow_id = client.post(
            "/api/studio/v1/workflows", json={"name": "provider-probe"}
        ).json()["id"]
        saved = client.put(
            f"/api/studio/v1/workflows/{workflow_id}",
            json={
                "entry_step": "start",
                "nodes": [
                    {
                        "id": "start",
                        "type": "entrypoint",
                        "position": {"x": 0, "y": 0},
                        "data": {
                            "label": "Start",
                            "config": {},
                            "input_contract_ref": "probe-contract",
                            "output_contract_ref": "probe-contract",
                        },
                    },
                    {
                        "id": "answer",
                        "type": "agent",
                        "position": {"x": 200, "y": 0},
                        "data": {
                            "label": "Answer",
                            "input_contract_ref": "probe-contract",
                            "output_contract_ref": "probe-contract",
                            "config": {
                                "instruction": "Answer briefly",
                                "model_provider": "openai/gpt-4o-mini",
                            },
                        },
                    },
                ],
                "edges": [
                    {"id": "answer-edge", "source": "start", "target": "answer"}
                ],
            },
        )
        assert saved.status_code == 200, saved.text

        refused = client.post(
            f"/api/studio/v1/workflows/{workflow_id}/verify-provider",
            json={"acknowledge_external_call": False},
        )
        assert refused.status_code == 422
        assert adapter.requests == []

        app.state.bootstrap.evaluation_campaign_id = "campaign-1"
        missing_campaign_identity = client.post(
            f"/api/studio/v1/workflows/{workflow_id}/verify-provider",
            json={"acknowledge_external_call": True},
        )
        assert missing_campaign_identity.status_code == 422
        assert adapter.requests == []

        verified = client.post(
            f"/api/studio/v1/workflows/{workflow_id}/verify-provider",
            json={
                "acknowledge_external_call": True,
                "timeout_seconds": 2,
                "max_models": 1,
                "campaign_id": "campaign-1",
                "operation_id": "provider-check",
                "run_id": "run-1",
                "max_cost_usd": "0.02",
                "run_cap_usd": "0.25",
            },
        )
        assert verified.status_code == 200, verified.text
        assert verified.json()["verified"] is True
        assert verified.json()["probes"][0]["model"] == "openai/gpt-4o-mini"
        assert verified.json()["probes"][0] | {
            "cost_event_id": "cost-event-provider",
            "audit_event_id": "audit_cost-event-provider",
            "cost_measurement": "estimated",
            "estimated_cost_usd": "0.01",
            "provider_request_id": "provider-request-1",
            "cleanup_status": "complete",
        } == verified.json()["probes"][0]
        assert verified.json()["campaign_id"] == "campaign-1"
        assert verified.json()["operation_id"] == "provider-check"
        assert instrumentation.reserved[0]["operation_id"] == "provider-check"
        assert instrumentation.reserved[0]["max_cost_usd"] == "0.01"
        assert instrumentation.reserved[0]["run_cap_usd"] == "0.25"
        assert instrumentation.committed[0]["campaign_id"] == "campaign-1"
        assert len(adapter.requests) == 1
        assert adapter.requests[0].model_params.max_tokens == 4

        app.state.bootstrap.provider_verification_adapter = DeterministicProviderAdapter(
            [ProviderResponse(content="OK")]
        )
        incomplete = client.post(
            f"/api/studio/v1/workflows/{workflow_id}/verify-provider",
            json={
                "acknowledge_external_call": True,
                "campaign_id": "campaign-1",
                "operation_id": "provider-check-incomplete",
                "run_id": "run-2",
                "max_cost_usd": "0.02",
                "run_cap_usd": "0.25",
            },
        )
        assert incomplete.status_code == 200
        assert incomplete.json()["verified"] is False
        assert incomplete.json()["probes"][0]["error_code"] == "incomplete_measurement"
        assert instrumentation.ambiguous[-1]["operation_id"] == "provider-check-incomplete"


class TestCloneAndDraftGuard:
    """POST .../clone and the draft-only edit guard."""

    def test_clone_published_to_draft(self) -> None:
        import asyncio

        repo = _make_repo()
        app = _make_app(repo)
        client = TestClient(app)
        wf_id = client.post("/api/studio/v1/workflows", json={"name": "pub"}).json()["id"]
        asyncio.run(repo.publish(wf_id))

        resp = client.post(f"/api/studio/v1/workflows/{wf_id}/clone")
        assert resp.status_code == 201, resp.text
        assert resp.json()["status"] == "draft"
        assert resp.json()["version"] == 2

    def test_clone_draft_rejected(self) -> None:
        repo = _make_repo()
        app = _make_app(repo)
        client = TestClient(app)
        wf_id = client.post("/api/studio/v1/workflows", json={"name": "d"}).json()["id"]
        resp = client.post(f"/api/studio/v1/workflows/{wf_id}/clone")
        assert resp.status_code == 409

    def test_edit_published_rejected(self) -> None:
        import asyncio

        repo = _make_repo()
        app = _make_app(repo)
        client = TestClient(app)
        wf_id = client.post("/api/studio/v1/workflows", json={"name": "p"}).json()["id"]
        asyncio.run(repo.publish(wf_id))

        resp = client.put(f"/api/studio/v1/workflows/{wf_id}", json={"name": "nope"})
        assert resp.status_code == 409


def _branching_graph(metadata: dict | None = None):
    """A 3-node branching graph (entry a -> b, c) for layout tests."""
    from zeroth.contracts.graph.models import (
        AgentNode,
        AgentNodeData,
        Edge,
        ExecutableUnitNode,
        ExecutableUnitNodeData,
        Graph,
    )

    return Graph(
        graph_id="layout-graph",
        name="Layout graph",
        version=1,
        entry_step="a",
        nodes=[
            AgentNode(
                node_id="a",
                graph_version_ref="layout-graph@1",
                agent=AgentNodeData(instruction="x", model_provider="openai/gpt-4o-mini"),
            ),
            ExecutableUnitNode(
                node_id="b",
                graph_version_ref="layout-graph@1",
                executable_unit=ExecutableUnitNodeData(
                    manifest_ref="eu://echo", execution_mode="native"
                ),
            ),
            ExecutableUnitNode(
                node_id="c",
                graph_version_ref="layout-graph@1",
                executable_unit=ExecutableUnitNodeData(
                    manifest_ref="eu://echo", execution_mode="native"
                ),
            ),
        ],
        edges=[
            Edge(edge_id="e1", source_node_id="a", target_node_id="b"),
            Edge(edge_id="e2", source_node_id="a", target_node_id="c"),
        ],
        metadata=metadata or {},
    )


class TestStudioAutoLayout:
    """A graph deployed outside the Studio carries no positions; the canvas must.

    not stack every node at the origin.
    """

    def test_no_positions_gets_non_overlapping_layout(self) -> None:
        from zeroth.service.api.studio_api import _graph_to_detail

        detail = _graph_to_detail(_branching_graph())
        positions = {n.id: (n.position.x, n.position.y) for n in detail.nodes}
        # Three distinct positions (not all stacked at the origin).
        assert len(set(positions.values())) == 3
        # Entry at x=0; downstream branches laid out to the right.
        assert positions["a"][0] == 0
        assert positions["b"][0] > 0
        assert positions["c"][0] > 0
        # Siblings at the same depth are separated vertically.
        assert positions["b"][1] != positions["c"][1]

    def test_stored_positions_win_over_auto_layout(self) -> None:
        from zeroth.service.api.studio_api import _graph_to_detail

        graph = _branching_graph(metadata={"studio": {"node_positions": {"a": {"x": 5, "y": 7}}}})
        detail = _graph_to_detail(graph)
        positions = {n.id: (n.position.x, n.position.y) for n in detail.nodes}
        # Stored position for 'a' is respected verbatim; the un-positioned nodes
        # b/c are filled from auto-layout rather than stacking at the origin.
        assert positions["a"] == (5, 7)
        assert positions["b"] != (0, 0)
        assert positions["c"] != (0, 0)
        assert positions["b"] != positions["c"]

    def test_no_entry_step_does_not_collapse_to_one_column(self) -> None:
        from zeroth.service.api.studio_api import _auto_layout

        graph = _branching_graph().model_copy(update={"entry_step": ""})  # falsy entry_step
        layout = _auto_layout(graph)
        # Not every node stacked in the x=0 column.
        assert len({p["x"] for p in layout.values()}) > 1


class TestAuthorization:
    """Route-level RBAC on the Studio surface (require_permission)."""

    def test_reviewer_cannot_create_workflow(self) -> None:
        repo = _make_repo()
        app = _make_app(repo, roles=[ServiceRole.REVIEWER])
        client = TestClient(app)
        resp = client.post("/api/studio/v1/workflows", json={"name": "x"})
        assert resp.status_code == 403

    def test_reviewer_can_list_workflows(self) -> None:
        repo = _make_repo()
        app = _make_app(repo, roles=[ServiceRole.REVIEWER])
        client = TestClient(app)
        resp = client.get("/api/studio/v1/workflows")
        assert resp.status_code == 200

    def test_operator_can_create_workflow(self) -> None:
        repo = _make_repo()
        app = _make_app(repo, roles=[ServiceRole.OPERATOR])
        client = TestClient(app)
        resp = client.post("/api/studio/v1/workflows", json={"name": "x"})
        assert resp.status_code == 201

    def test_roleless_principal_cannot_read_or_write(self) -> None:
        repo = _make_repo()
        app = _make_app(repo, roles=[])
        client = TestClient(app)
        assert client.get("/api/studio/v1/workflows").status_code == 403
        assert client.post("/api/studio/v1/workflows", json={"name": "x"}).status_code == 403
        assert client.get("/api/studio/v1/node-types").status_code == 403
