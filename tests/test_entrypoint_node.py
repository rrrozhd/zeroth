"""Entrypoint node — the spatial home of entry_step and the run input contract.

Covers the structural validator rules, the studio auto-derivation of
entry_step, and the studio publish gate requiring canvas workflows to start
with an Entrypoint node.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.test_studio_publish_deploy import _make_env, _register_contracts
from zeroth.contracts.graph.models import (
    AgentNode,
    AgentNodeData,
    Edge,
    EntrypointNode,
    Graph,
)
from zeroth.runtime.graph_validation import GraphValidator

_REF = "wf@1"


def _entry(node_id: str = "start") -> EntrypointNode:
    return EntrypointNode(
        node_id=node_id,
        graph_version_ref=_REF,
        input_contract_ref="contract://q",
        output_contract_ref="contract://q",
    )


def _agent(node_id: str = "agent") -> AgentNode:
    return AgentNode(
        node_id=node_id,
        graph_version_ref=_REF,
        input_contract_ref="contract://q",
        output_contract_ref="contract://a",
        agent=AgentNodeData(instruction="answer", model_provider="openai/gpt-4o-mini"),
    )


def _graph(nodes, edges, entry_step) -> Graph:
    return Graph(graph_id="wf", name="wf", nodes=nodes, edges=edges, entry_step=entry_step)


# ---- validator rules ---------------------------------------------------------


async def test_single_entrypoint_wired_as_entry_is_valid() -> None:
    graph = _graph(
        [_entry(), _agent()],
        [Edge(edge_id="e1", source_node_id="start", target_node_id="agent")],
        entry_step="start",
    )
    report = await GraphValidator().validate(graph)
    assert report.is_valid, report.issues


async def test_entry_step_must_point_at_the_entrypoint_node() -> None:
    graph = _graph(
        [_entry(), _agent()],
        [Edge(edge_id="e1", source_node_id="start", target_node_id="agent")],
        entry_step="agent",
    )
    report = await GraphValidator().validate(graph)
    assert any(
        i.code.value == "unknown_entrypoint" and "entrypoint node" in i.message
        for i in report.errors
    ), report.issues


async def test_only_one_entrypoint_node_allowed() -> None:
    graph = _graph(
        [_entry("start"), _entry("start2"), _agent()],
        [Edge(edge_id="e1", source_node_id="start", target_node_id="agent")],
        entry_step="start",
    )
    report = await GraphValidator().validate(graph)
    assert any(
        i.node_id == "start2" and "one entrypoint" in i.message for i in report.errors
    ), report.issues


async def test_entrypoint_cannot_have_incoming_edges() -> None:
    graph = _graph(
        [_entry(), _agent()],
        [
            Edge(edge_id="e1", source_node_id="start", target_node_id="agent"),
            Edge(edge_id="e2", source_node_id="agent", target_node_id="start"),
        ],
        entry_step="start",
    )
    report = await GraphValidator().validate(graph)
    assert any(
        i.edge_id == "e2" and "incoming" in i.message for i in report.errors
    ), report.issues


# ---- studio behavior ----------------------------------------------------------


def _entry_node_payload(node_id: str = "start") -> dict:
    return {
        "id": node_id,
        "type": "entrypoint",
        "position": {"x": 0, "y": 0},
        "data": {
            "label": "Start",
            "input_contract_ref": "contract://q",
            "output_contract_ref": "contract://q",
            "config": {},
        },
    }


def _agent_node_payload(node_id: str = "agent") -> dict:
    return {
        "id": node_id,
        "type": "agent",
        "position": {"x": 200, "y": 0},
        "data": {
            "label": "Agent",
            "input_contract_ref": "contract://q",
            "output_contract_ref": "contract://a",
            "config": {"instruction": "answer", "model_provider": "openai/gpt-4o-mini"},
        },
    }


async def test_entrypoint_node_owns_entry_step_and_publishes() -> None:
    app, registry = _make_env()
    await _register_contracts(registry)

    with TestClient(app) as client:
        created = client.post("/api/studio/v1/workflows", json={"name": "wf"}).json()
        wf = f"/api/studio/v1/workflows/{created['id']}"

        updated = client.put(
            wf,
            json={
                "nodes": [_entry_node_payload(), _agent_node_payload()],
                "edges": [{"id": "e1", "source": "start", "target": "agent"}],
                # Deliberately wrong: the entrypoint node must win.
                "entry_step": "agent",
            },
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["entry_step"] == "start"
        assert {n["type"] for n in updated.json()["nodes"]} == {"entrypoint", "agent"}

        published = client.post(f"{wf}/publish")
        assert published.status_code == 200, published.text


async def test_studio_publish_requires_an_entrypoint_node() -> None:
    app, registry = _make_env()
    await _register_contracts(registry)

    with TestClient(app) as client:
        created = client.post("/api/studio/v1/workflows", json={"name": "wf"}).json()
        wf = f"/api/studio/v1/workflows/{created['id']}"
        client.put(
            wf,
            json={
                "nodes": [_agent_node_payload()],
                "edges": [],
                "entry_step": "agent",
            },
        )
        published = client.post(f"{wf}/publish")
        assert published.status_code == 422
        issues = published.json()["detail"]["issues"]
        assert any(i["code"] == "missing_entrypoint_node" for i in issues), issues
