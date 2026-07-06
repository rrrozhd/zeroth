"""Tests for Studio graph authoring REST API."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from zeroth.core.graph.repository import GraphRepository
from zeroth.core.service.bootstrap import run_migrations
from zeroth.core.service.studio_api import router as studio_router
from zeroth.core.storage.async_sqlite import AsyncSQLiteDatabase


def _make_app(graph_repo: GraphRepository | None = None) -> FastAPI:
    """Create a minimal FastAPI app with Studio routes and no auth middleware."""
    app = FastAPI()
    bootstrap = MagicMock()
    if graph_repo is not None:
        bootstrap.graph_repository = graph_repo
    else:
        bootstrap.graph_repository = MagicMock(spec=GraphRepository)
    app.state.bootstrap = bootstrap
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
    """POST /api/studio/v1/workflows"""

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
        assert "nodes" in data
        assert "edges" in data
        assert "viewport" in data

    def test_create_workflow_empty_name_rejected(self) -> None:
        repo = _make_repo()
        app = _make_app(repo)
        client = TestClient(app)

        resp = client.post("/api/studio/v1/workflows", json={"name": ""})
        assert resp.status_code == 422


class TestListWorkflows:
    """GET /api/studio/v1/workflows"""

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
    """GET /api/studio/v1/workflows/{workflow_id}"""

    def test_get_workflow(self) -> None:
        repo = _make_repo()
        app = _make_app(repo)
        client = TestClient(app)

        create_resp = client.post(
            "/api/studio/v1/workflows", json={"name": "detail-test"}
        )
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
    """PUT /api/studio/v1/workflows/{workflow_id}"""

    def test_update_workflow_name(self) -> None:
        repo = _make_repo()
        app = _make_app(repo)
        client = TestClient(app)

        create_resp = client.post(
            "/api/studio/v1/workflows", json={"name": "original"}
        )
        wf_id = create_resp.json()["id"]

        resp = client.put(
            f"/api/studio/v1/workflows/{wf_id}", json={"name": "updated"}
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "updated"

    def test_update_workflow_not_found(self) -> None:
        repo = _make_repo()
        app = _make_app(repo)
        client = TestClient(app)

        resp = client.put(
            "/api/studio/v1/workflows/nonexistent", json={"name": "x"}
        )
        assert resp.status_code == 404


class TestDeleteWorkflow:
    """DELETE /api/studio/v1/workflows/{workflow_id}"""

    def test_delete_workflow(self) -> None:
        repo = _make_repo()
        app = _make_app(repo)
        client = TestClient(app)

        create_resp = client.post(
            "/api/studio/v1/workflows", json={"name": "to-delete"}
        )
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
    """GET /api/studio/v1/node-types"""

    def test_list_node_types(self) -> None:
        repo = _make_repo()
        app = _make_app(repo)
        client = TestClient(app)

        resp = client.get("/api/studio/v1/node-types")
        assert resp.status_code == 200
        data = resp.json()
        # The palette mirrors the executable graph model's node_type discriminator.
        type_names = {item["type"] for item in data}
        assert type_names == {
            "agent",
            "executable_unit",
            "human_approval",
            "retrieval",
            "subgraph",
        }
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


class TestStructuralAuthoring:
    """PUT persists real executable nodes/edges (not just visual metadata)."""

    def test_persist_nodes_and_edges_round_trip(self) -> None:
        repo = _make_repo()
        app = _make_app(repo)
        client = TestClient(app)

        wf_id = client.post("/api/studio/v1/workflows", json={"name": "authored"}).json()[
            "id"
        ]

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

    def test_edge_to_unknown_node_rejected(self) -> None:
        repo = _make_repo()
        app = _make_app(repo)
        client = TestClient(app)
        wf_id = client.post("/api/studio/v1/workflows", json={"name": "dangling"}).json()[
            "id"
        ]
        resp = client.put(
            f"/api/studio/v1/workflows/{wf_id}",
            json={
                "nodes": [],
                "edges": [{"id": "e", "source": "ghost", "target": "ghost2"}],
            },
        )
        assert resp.status_code == 422


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
    from zeroth.core.graph.models import (
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
    """A graph deployed outside the Studio carries no positions; the canvas must
    not stack every node at the origin."""

    def test_no_positions_gets_non_overlapping_layout(self) -> None:
        from zeroth.core.service.studio_api import _graph_to_detail

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
        from zeroth.core.service.studio_api import _graph_to_detail

        graph = _branching_graph(
            metadata={"studio": {"node_positions": {"a": {"x": 5, "y": 7}}}}
        )
        detail = _graph_to_detail(graph)
        positions = {n.id: (n.position.x, n.position.y) for n in detail.nodes}
        # Stored position for 'a' is respected verbatim; the un-positioned nodes
        # b/c are filled from auto-layout rather than stacking at the origin.
        assert positions["a"] == (5, 7)
        assert positions["b"] != (0, 0)
        assert positions["c"] != (0, 0)
        assert positions["b"] != positions["c"]

    def test_no_entry_step_does_not_collapse_to_one_column(self) -> None:
        from zeroth.core.service.studio_api import _auto_layout

        graph = _branching_graph().model_copy(update={"entry_step": ""})  # falsy entry_step
        layout = _auto_layout(graph)
        # Not every node stacked in the x=0 column.
        assert len({p["x"] for p in layout.values()}) > 1
