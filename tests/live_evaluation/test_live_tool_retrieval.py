from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from release.live_evaluation.live_tool_retrieval import (
    LiveToolRetrievalFixture,
    provision_live_tool_retrieval_fixture,
    validate_live_tool_retrieval_observation,
)


@dataclass
class _Response:
    status_code: int
    payload: object
    text: str = "<sanitized>"

    def json(self) -> object:
        return self.payload


def test_provision_builds_visible_read_only_tool_edge_and_real_retrieval() -> None:
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def request(method: str, path: str, payload: dict[str, Any] | None) -> _Response:
        calls.append((method, path, payload))
        if path == "/api/studio/v1/contracts":
            assert payload is not None
            return _Response(201, {"name": payload["name"], "version": 1})
        if path == "/api/studio/v1/workflows" and method == "POST":
            return _Response(201, {"id": "tool-retrieval-workflow"})
        if path.endswith("/preflight"):
            return _Response(200, {"ready": True, "issues": []})
        if path.endswith("/publish"):
            return _Response(200, {"status": "published", "version": 1})
        if path == "/v1/deployments":
            return _Response(
                201,
                {
                    "deployment_ref": "live-tool-retrieval-w1-tool-1",
                    "version": 1,
                    "graph_version_ref": "tool-retrieval-workflow@1",
                },
            )
        return _Response(200, {"id": "tool-retrieval-workflow"})

    fixture = provision_live_tool_retrieval_fixture(
        request=request,
        fixture_id="w1-tool-1",
        connector_ref="eval_chroma_v1",
    )

    assert fixture == LiveToolRetrievalFixture(
        fixture_id="w1-tool-1",
        workflow_id="tool-retrieval-workflow",
        graph_version_ref="tool-retrieval-workflow@1",
        deployment_ref="live-tool-retrieval-w1-tool-1",
        deployment_version=1,
        connector_ref="eval_chroma_v1",
    )
    saved = next(payload for method, path, payload in calls if method == "PUT")
    assert saved is not None
    nodes = {node["id"]: node for node in saved["nodes"]}
    agent = nodes["grounded-agent"]["data"]["config"]
    assert agent["model_provider"] == "openai/gpt-4o-mini"
    assert agent["max_tool_calls"] == 1
    assert agent["tool_bindings"] == [
        {
            "target_node_id": "status-tool",
            "name": "get_retrieval_index_status",
            "description": "Read the local evaluation retrieval index status and receipt.",
            "arguments": [
                {
                    "name": "service",
                    "type": "string",
                    "description": "The exact local service name retrieval-index.",
                    "required": True,
                }
            ],
        }
    ]
    assert nodes["retrieve"]["data"]["config"] == {
        "connector_ref": "eval_chroma_v1",
        "query_key": "query",
        "top_k": 3,
        "scope": "shared",
        "as_name": "sources",
    }
    tool = nodes["status-tool"]
    assert tool["type"] == "code"
    assert "retrieval-index-live" in tool["data"]["config"]["inline_source"]
    assert any(
        edge["kind"] == "tool"
        and edge["source"] == "grounded-agent"
        and edge["target"] == "status-tool"
        for edge in saved["edges"]
    )


def test_observation_requires_exact_retrieval_tool_audit_and_economics() -> None:
    fixture = LiveToolRetrievalFixture(
        fixture_id="w1-tool-1",
        workflow_id="tool-retrieval-workflow",
        graph_version_ref="tool-retrieval-workflow@1",
        deployment_ref="live-tool-retrieval-w1-tool-1",
        deployment_version=1,
        connector_ref="eval_chroma_v1",
    )
    observation = {
        "fixture_id": fixture.fixture_id,
        "deployment_ref": fixture.deployment_ref,
        "graph_version_ref": fixture.graph_version_ref,
        "run_id": "run-live-tool-1",
        "status": "succeeded",
        "output": {
            "answer": "The approved queue depth is four and the retrieval index is healthy.",
            "source_ids": ["evaluation-ground-truth-beta"],
            "tool_receipt": "retrieval-index-live",
        },
        "retrieval": {
            "node_id": "retrieve",
            "result_ids": ["evaluation-ground-truth-beta"],
        },
        "tool_calls": [
            {
                "node_id": "grounded-agent",
                "tool": "get_retrieval_index_status",
                "arguments": {"service": "retrieval-index"},
                "outcome": {
                    "service": "retrieval-index",
                    "status": "healthy",
                    "receipt": "retrieval-index-live",
                },
            }
        ],
        "provider_events": [
            {
                "model": "openai/text-embedding-3-small",
                "cost_event_id": "embedding-cost-1",
                "measured_cost_usd": "0.000001",
            },
            {
                "model": "openai/gpt-4o-mini",
                "cost_event_id": "chat-cost-1",
                "measured_cost_usd": "0.00001",
            },
            {
                "model": "openai/gpt-4o-mini",
                "cost_event_id": "chat-cost-2",
                "measured_cost_usd": "0.00001",
            },
        ],
        "audit": {"signed": True, "chain_verified": True, "tool_call_count": 1},
        "economics": {
            "audit_total_usd": "0.000021",
            "ledger_total_usd": "0.000021",
            "regulus_total_usd": "0.000021",
            "ambiguous_exposure_usd": "0",
        },
    }

    result = validate_live_tool_retrieval_observation(observation, expected=fixture)
    assert result["run_id"] == "run-live-tool-1"
    assert result["provider_event_count"] == 3


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["tool_calls"].clear(),
        lambda value: value["audit"].update({"chain_verified": False}),
        lambda value: value["economics"].update({"ledger_total_usd": "0.1"}),
    ],
)
def test_observation_fails_closed_on_missing_correlation(
    mutation: Any,
) -> None:
    fixture = LiveToolRetrievalFixture(
        fixture_id="w1-tool-1",
        workflow_id="tool-retrieval-workflow",
        graph_version_ref="tool-retrieval-workflow@1",
        deployment_ref="live-tool-retrieval-w1-tool-1",
        deployment_version=1,
        connector_ref="eval_chroma_v1",
    )
    observation: dict[str, Any] = {
        "fixture_id": fixture.fixture_id,
        "deployment_ref": fixture.deployment_ref,
        "graph_version_ref": fixture.graph_version_ref,
        "run_id": "run-live-tool-1",
        "status": "succeeded",
        "output": {
            "answer": "The approved queue depth is four and the retrieval index is healthy.",
            "source_ids": ["evaluation-ground-truth-beta"],
            "tool_receipt": "retrieval-index-live",
        },
        "retrieval": {
            "node_id": "retrieve",
            "result_ids": ["evaluation-ground-truth-beta"],
        },
        "tool_calls": [
            {
                "node_id": "grounded-agent",
                "tool": "get_retrieval_index_status",
                "arguments": {"service": "retrieval-index"},
                "outcome": {
                    "service": "retrieval-index",
                    "status": "healthy",
                    "receipt": "retrieval-index-live",
                },
            }
        ],
        "provider_events": [
            {
                "model": "openai/text-embedding-3-small",
                "cost_event_id": "embedding-cost-1",
                "measured_cost_usd": "0.000001",
            },
            {
                "model": "openai/gpt-4o-mini",
                "cost_event_id": "chat-cost-1",
                "measured_cost_usd": "0.00001",
            },
        ],
        "audit": {"signed": True, "chain_verified": True, "tool_call_count": 1},
        "economics": {
            "audit_total_usd": "0.000011",
            "ledger_total_usd": "0.000011",
            "regulus_total_usd": "0.000011",
            "ambiguous_exposure_usd": "0",
        },
    }
    mutation(observation)
    with pytest.raises(RuntimeError):
        validate_live_tool_retrieval_observation(observation, expected=fixture)
