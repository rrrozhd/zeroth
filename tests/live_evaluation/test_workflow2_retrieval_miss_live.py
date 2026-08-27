from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
import pytest

from release.live_evaluation.workflow2_retrieval_miss_live import (
    RETRIEVAL_ITEMS,
    ProviderFreeWorkflow2RetrievalMissFixture,
    provision_workflow2_retrieval_miss_fixture,
    validate_workflow2_retrieval_miss_summary,
)


class _Response:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self) -> object:
        return self._payload


def _request_recorder() -> tuple[
    list[tuple[str, str, dict[str, Any] | None]],
    Any,
]:
    calls: list[tuple[str, str, dict[str, Any] | None]] = []
    workflow_ids = iter(("retrieval-child", "retrieval-parent"))

    def request(method: str, path: str, payload: dict[str, Any] | None = None) -> _Response:
        calls.append((method, path, payload))
        if method == "POST" and path == "/api/studio/v1/contracts":
            assert payload is not None
            return _Response(201, {"name": payload["name"], "version": 1})
        if method == "POST" and path == "/api/studio/v1/workflows":
            return _Response(201, {"id": next(workflow_ids), "status": "draft", "version": 1})
        if method == "PUT" and path.startswith("/api/studio/v1/workflows/"):
            return _Response(200, {"id": path.rsplit("/", 1)[-1], "status": "draft"})
        if method == "POST" and path.endswith("/preflight"):
            return _Response(200, {"ready": True, "issues": []})
        if method == "POST" and path.endswith("/publish"):
            return _Response(200, {"status": "published", "version": 1})
        if method == "POST" and path == "/v1/deployments":
            assert payload is not None
            return _Response(
                201,
                {
                    "deployment_ref": payload["deployment_ref"],
                    "version": 1,
                    "graph_version_ref": f"{payload['graph_id']}@1",
                },
            )
        raise AssertionError((method, path, payload))

    return calls, request


def test_fixture_is_exact_eight_concurrency_four_with_one_local_retrieval_path() -> None:
    calls, request = _request_recorder()

    fixture = provision_workflow2_retrieval_miss_fixture(
        request=request,
        fixture_id="w2-retrieval-live",
    )

    assert isinstance(fixture, ProviderFreeWorkflow2RetrievalMissFixture)
    assert fixture.items == RETRIEVAL_ITEMS
    assert fixture.retrieval_miss_branch_index == 3
    assert fixture.max_concurrency == 4
    assert fixture.provider_calls_performed == 0
    saves = [payload for method, path, payload in calls if method == "PUT"]
    assert len(saves) == 2
    child, parent = saves
    assert [node["type"] for node in child["nodes"]] == [
        "entrypoint",
        "if",
        "retrieval",
        "code",
        "code",
    ]
    decision = child["nodes"][1]
    assert decision["data"]["config"] == {"expression": "payload.index == 3"}
    routes = {
        edge["source_handle"]: edge["target"]
        for edge in child["edges"]
        if edge["source"] == "retrieval-decision"
    }
    assert routes == {"true": "local-retrieval", "false": "complete-sibling"}
    retrieval = child["nodes"][2]
    assert retrieval["data"]["config"] == {
        "connector_ref": "ephemeral",
        "query_key": "query",
        "top_k": 1,
        "scope": "run",
        "as_name": "retrieved",
    }
    assert retrieval["data"]["capability_bindings"] == ["memory_read"]
    assert "deterministic retrieval miss" in child["nodes"][3]["data"]["config"]["inline_source"]
    parallel = parent["nodes"][0]["data"]["parallel_config"]
    assert parallel == {
        "split_path": "items",
        "merge_strategy": "collect",
        "fail_mode": "best_effort",
        "max_branches": 8,
        "max_concurrency": 4,
        "batch_size": 8,
        "branch_timeout_seconds": 30,
    }
    assert parent["nodes"][1]["data"]["config"] == {
        "graph_ref": fixture.child_deployment_ref,
        "version": 1,
        "thread_participation": "isolated",
        "max_depth": 1,
    }
    assert sum(node["type"] == "retrieval" for save in saves for node in save["nodes"]) == 1
    assert all(node["type"] != "agent" for save in saves for node in save["nodes"])


def test_fixture_publishes_and_deploys_through_real_studio_apis() -> None:
    from tests.test_studio_publish_deploy import _make_env

    app, _ = _make_env()
    app.state.bootstrap.memory_registry = MagicMock()
    app.state.bootstrap.memory_registry.list.return_value = {"ephemeral": object()}

    with TestClient(app) as client:

        def request(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
            route = "/deployments" if path == "/v1/deployments" else path
            return client.request(method, route, json=payload)

        fixture = provision_workflow2_retrieval_miss_fixture(
            request=request,
            fixture_id="retrieval-real-api",
        )
        deployments = client.get("/deployments")

    assert fixture.child_graph_version_ref == f"{fixture.child_workflow_id}@1"
    assert fixture.parent_graph_version_ref == f"{fixture.parent_workflow_id}@1"
    assert deployments.status_code == 200
    assert {row["deployment_ref"] for row in deployments.json()} == {
        fixture.child_deployment_ref,
        fixture.parent_deployment_ref,
    }


def _summary() -> dict[str, object]:
    parent = "parent-retrieval-miss"
    children = [
        {
            "run_id": f"child-{index}",
            "thread_id": f"thread-{index}",
            "parent_run_id": parent,
            "branch_index": index,
            "status": "failed" if index == 3 else "succeeded",
            "failure_reason": "node_execution_failed" if index == 3 else None,
        }
        for index in range(8)
    ]
    output = [dict(item) for item in RETRIEVAL_ITEMS]
    output[3] = None
    return {
        "schema_version": 1,
        "provider_calls_performed": 0,
        "provider_request_ids": [],
        "cost_event_ids": [],
        "priced_call_count": 0,
        "total_cost_usd": 0,
        "configured_max_concurrency": 4,
        "retrieval_miss_branch_index": 3,
        "health": {
            "status": "ok",
            "deployment_ref": "retrieval-parent-deployment",
            "graph_version_ref": "retrieval-parent@1",
        },
        "parent": {
            "run_id": parent,
            "thread_id": "parent-thread",
            "status": "succeeded",
            "terminal_output": {"items": output},
        },
        "children": children,
        "retrieval_miss": {
            "child_run_id": "child-3",
            "retrieval_node_id": ("branch:3:subgraph:retrieval-child-deployment:1:local-retrieval"),
            "retrieval_result_count": 0,
            "failure_node_id": (
                "branch:3:subgraph:retrieval-child-deployment:1:require-retrieval-hit"
            ),
            "failure_reason": "node_execution_failed",
        },
        "refresh": {
            "before_parent_run_id": parent,
            "restored_parent_run_id": parent,
            "before_child_run_ids": [f"child-{index}" for index in range(8)],
            "restored_child_run_ids": [f"child-{index}" for index in range(8)],
        },
        "audit": {
            "signed_parent_chain": True,
            "signed_child_chain_count": 8,
            "unsigned_record_count": 0,
            "parent_run_id": parent,
            "child_parent_links": [
                {"child_run_id": f"child-{index}", "parent_run_id": parent} for index in range(8)
            ],
        },
    }


def test_summary_requires_one_retrieval_miss_seven_successes_and_zero_provider_activity() -> None:
    result = validate_workflow2_retrieval_miss_summary(
        _summary(),
        expected_deployment_ref="retrieval-parent-deployment",
        expected_graph_version_ref="retrieval-parent@1",
        expected_child_deployment_ref="retrieval-child-deployment",
        expected_child_deployment_version=1,
    )

    assert result == {
        "parent_run_id": "parent-retrieval-miss",
        "child_run_ids": [f"child-{index}" for index in range(8)],
        "successful_child_count": 7,
        "failed_child_count": 1,
        "retrieval_miss_branch_index": 3,
        "priced_call_count": 0,
        "total_cost_usd": 0.0,
        "provider_request_ids": [],
        "cost_event_ids": [],
        "refresh_restored": True,
    }


def test_summary_accepts_exact_refresh_identity_set_in_ui_order() -> None:
    summary = _summary()
    ui_order = [
        "child-6",
        "child-1",
        "child-4",
        "child-0",
        "child-7",
        "child-3",
        "child-5",
        "child-2",
    ]
    summary["refresh"]["before_child_run_ids"] = ui_order
    summary["refresh"]["restored_child_run_ids"] = ui_order

    result = validate_workflow2_retrieval_miss_summary(
        summary,
        expected_deployment_ref="retrieval-parent-deployment",
        expected_graph_version_ref="retrieval-parent@1",
        expected_child_deployment_ref="retrieval-child-deployment",
        expected_child_deployment_version=1,
    )

    assert result["refresh_restored"] is True


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda summary: summary["children"][0].update(status="failed"), "seven successes"),
        (
            lambda summary: summary["retrieval_miss"].update(retrieval_result_count=1),
            "zero-result retrieval",
        ),
        (lambda summary: summary.update(provider_request_ids=["provider-1"]), "provider"),
        (lambda summary: summary["refresh"].update(restored_parent_run_id="other"), "refresh"),
        (
            lambda summary: summary["refresh"].update(
                restored_child_run_ids=list(reversed(summary["refresh"]["before_child_run_ids"]))
            ),
            "refresh",
        ),
        (
            lambda summary: summary["audit"]["child_parent_links"][3].update(parent_run_id="other"),
            "signed parent/child",
        ),
    ],
)
def test_summary_fails_closed_on_drift(mutate: Any, message: str) -> None:
    summary = _summary()
    mutate(summary)

    with pytest.raises(RuntimeError, match=message):
        validate_workflow2_retrieval_miss_summary(
            summary,
            expected_deployment_ref="retrieval-parent-deployment",
            expected_graph_version_ref="retrieval-parent@1",
            expected_child_deployment_ref="retrieval-child-deployment",
            expected_child_deployment_version=1,
        )
