"""Studio API: tool-edge authoring — edge kind round-trip and tool ports."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.test_studio_api import _make_app, _make_repo


def _agent_and_code_nodes() -> list[dict]:
    return [
        {
            "id": "agent-1",
            "type": "agent",
            "position": {"x": 0, "y": 0},
            "data": {
                "label": "Agent",
                "config": {
                    "instruction": "use your tool",
                    "model_provider": "openai/gpt-4o",
                    "tool_bindings": [
                        {
                            "target_node_id": "code-1",
                            "name": "run_lookup",
                            "description": "Runs the lookup",
                            "arguments": [
                                {"name": "query", "description": "What to look up"}
                            ],
                        }
                    ],
                },
            },
        },
        {
            "id": "code-1",
            "type": "code",
            "position": {"x": 0, "y": 200},
            "data": {"label": "Lookup", "config": {"inline_source": "print('{}')"}},
        },
    ]


class TestToolEdgeAuthoring:
    def test_tool_edge_kind_round_trips(self) -> None:
        repo = _make_repo()
        client = TestClient(_make_app(repo))
        wf = client.post("/api/studio/v1/workflows", json={"name": "tools"}).json()

        resp = client.put(
            f"/api/studio/v1/workflows/{wf['id']}",
            json={
                "nodes": _agent_and_code_nodes(),
                "edges": [
                    {
                        "id": "t1",
                        "source": "agent-1",
                        "target": "code-1",
                        "source_handle": "tools",
                        "target_handle": "tool-input",
                        "kind": "tool",
                    }
                ],
            },
        )
        assert resp.status_code == 200, resp.text
        detail = client.get(f"/api/studio/v1/workflows/{wf['id']}").json()
        assert detail["edges"][0]["kind"] == "tool"
        agent = next(n for n in detail["nodes"] if n["id"] == "agent-1")
        assert agent["data"]["config"]["tool_bindings"][0]["name"] == "run_lookup"

    def test_tools_handle_implies_tool_kind_for_old_clients(self) -> None:
        repo = _make_repo()
        client = TestClient(_make_app(repo))
        wf = client.post("/api/studio/v1/workflows", json={"name": "tools"}).json()

        resp = client.put(
            f"/api/studio/v1/workflows/{wf['id']}",
            json={
                "nodes": _agent_and_code_nodes(),
                "edges": [
                    {
                        "id": "t1",
                        "source": "agent-1",
                        "target": "code-1",
                        "source_handle": "tools",
                        "target_handle": "tool-input",
                    }
                ],
            },
        )
        assert resp.status_code == 200, resp.text
        detail = client.get(f"/api/studio/v1/workflows/{wf['id']}").json()
        assert detail["edges"][0]["kind"] == "tool"

    def test_node_types_expose_tool_ports(self) -> None:
        client = TestClient(_make_app(_make_repo()))
        types = {t["type"]: t for t in client.get("/api/studio/v1/node-types").json()}

        agent_ports = {p["id"]: p for p in types["agent"]["ports"]}
        assert agent_ports["tools"]["type"] == "tool"
        assert agent_ports["tools"]["direction"] == "output"
        for unit_type in ("code", "executable_unit"):
            ports = {p["id"]: p for p in types[unit_type]["ports"]}
            assert ports["tool-input"]["type"] == "tool"
            assert ports["tool-input"]["direction"] == "input"
