from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from release.live_evaluation.live_provider_batch_fixture import (
    MODEL,
    ProviderBatchFixture,
    provision_provider_batch_fixture,
    write_unsealed_provider_batch_manifest,
)


@dataclass
class _Response:
    status_code: int
    payload: object
    text: str = "<sanitized>"

    def json(self) -> object:
        return self.payload


ITEMS = tuple(
    {"index": index, "query": f"Give a concise operational finding for case {index}."}
    for index in range(8)
)


def _provision() -> tuple[
    ProviderBatchFixture,
    list[tuple[str, str, dict[str, Any] | None]],
]:
    calls: list[tuple[str, str, dict[str, Any] | None]] = []
    workflow_ids = iter(("provider-batch-child-id", "provider-batch-parent-id"))

    def request(method: str, path: str, payload: dict[str, Any] | None) -> _Response:
        calls.append((method, path, payload))
        if path == "/api/studio/v1/contracts":
            assert method == "POST" and payload is not None
            return _Response(201, {"name": payload["name"], "version": 1})
        if path == "/api/studio/v1/workflows" and method == "POST":
            return _Response(201, {"id": next(workflow_ids)})
        if method == "PUT" and path.startswith("/api/studio/v1/workflows/"):
            return _Response(200, {"status": "saved"})
        if path.endswith("/preflight"):
            return _Response(200, {"ready": True, "issues": []})
        if path.endswith("/publish"):
            return _Response(200, {"status": "published", "version": 1})
        if path == "/v1/deployments":
            assert payload is not None
            workflow_id = payload["graph_id"]
            return _Response(
                201,
                {
                    "deployment_ref": payload["deployment_ref"],
                    "version": 1,
                    "graph_version_ref": f"{workflow_id}@1",
                },
            )
        raise AssertionError(f"unexpected request: {method} {path}")

    fixture = provision_provider_batch_fixture(
        request=request,
        fixture_id="provider-batch-v2",
        items=ITEMS,
    )
    return fixture, calls


def test_provision_builds_one_call_child_and_deterministic_eight_item_parent() -> None:
    fixture, calls = _provision()

    assert fixture == ProviderBatchFixture(
        schema_version=1,
        fixture_id="provider-batch-v2",
        child_workflow_id="provider-batch-child-id",
        child_graph_version_ref="provider-batch-child-id@1",
        child_deployment_ref="live-provider-batch-provider-batch-v2-child",
        child_deployment_version=1,
        parent_workflow_id="provider-batch-parent-id",
        parent_graph_version_ref="provider-batch-parent-id@1",
        parent_deployment_ref="live-provider-batch-provider-batch-v2-parent",
        parent_deployment_version=1,
        model=MODEL,
    )

    saves = [
        payload
        for method, path, payload in calls
        if method == "PUT" and path.startswith("/api/studio/v1/workflows/")
    ]
    assert len(saves) == 2
    child, parent = saves
    assert child is not None and parent is not None

    child_nodes = {node["id"]: node for node in child["nodes"]}
    assert set(child_nodes) == {"item", "analyze"}
    agent = child_nodes["analyze"]
    assert agent["type"] == "agent"
    config = agent["data"]["config"]
    assert config["model_provider"] == "openai/gpt-4o-mini"
    assert config["model_params"]["temperature"] == 0
    assert config["model_params"]["max_tokens"] >= 400
    assert config["max_tool_calls"] == 0
    assert config["tool_refs"] == []
    assert config["tool_bindings"] == []
    assert "Preserve index exactly" in config["instruction"]
    assert "at most two sentences" in config["instruction"]
    assert child["edges"] == [
        {"id": "item-analyze", "source": "item", "target": "analyze", "kind": "data"}
    ]

    parent_nodes = {node["id"]: node for node in parent["nodes"]}
    assert set(parent_nodes) == {"batch", "analyze-child", "ordered-join"}
    parallel = parent_nodes["batch"]["data"]["parallel_config"]
    assert parallel == {
        "split_path": "items",
        "merge_strategy": "collect",
        "fail_mode": "fail_fast",
        "max_branches": 8,
        "max_concurrency": 4,
        "batch_size": 8,
        "branch_timeout_seconds": 90,
    }
    subgraph = parent_nodes["analyze-child"]["data"]["config"]
    assert subgraph == {
        "graph_ref": fixture.child_deployment_ref,
        "version": 1,
        "thread_participation": "isolated",
        "max_depth": 1,
    }
    join = parent_nodes["ordered-join"]
    assert join["type"] == "code"
    assert join["data"]["join_config"] == {
        "merge_strategy": "collect",
        "merge_path": "items",
    }
    assert "sorted(payload['items'], key=lambda row: row['index'])" in join["data"]["config"][
        "inline_source"
    ]
    assert parent["edges"] == [
        {
            "id": "batch-child",
            "source": "batch",
            "target": "analyze-child",
            "kind": "data",
        },
        {
            "id": "child-join",
            "source": "analyze-child",
            "target": "ordered-join",
            "kind": "data",
        },
    ]


def test_provision_uses_only_public_authoring_and_deployment_endpoints() -> None:
    fixture, calls = _provision()

    assert fixture.provider_calls_performed == 0
    assert fixture.provider_calls_per_child == 1
    assert fixture.configured_items == 8
    assert fixture.configured_concurrency == 4
    assert fixture.activated is False
    assert fixture.invoked is False
    assert fixture.sealed is False
    assert all(
        path == "/api/studio/v1/contracts"
        or path == "/api/studio/v1/workflows"
        or path == "/v1/deployments"
        or path.startswith("/api/studio/v1/workflows/")
        for _, path, _ in calls
    )
    assert not any("run" in path or "activate" in path or "restart" in path for _, path, _ in calls)


def test_contracts_separate_batch_input_collected_children_and_ordered_result() -> None:
    _, calls = _provision()
    contracts = [
        payload
        for method, path, payload in calls
        if method == "POST" and path == "/api/studio/v1/contracts"
    ]
    assert len(contracts) == 4
    schemas = {payload["name"].rsplit(".", 1)[-1]: payload["json_schema"] for payload in contracts}
    assert schemas["item"]["required"] == ["index", "query"]
    assert schemas["result"]["required"] == ["index", "query", "finding", "confidence"]
    assert schemas["batch"]["properties"]["items"]["minItems"] == 8
    assert schemas["batch"]["properties"]["items"]["maxItems"] == 8
    assert schemas["collected"]["properties"]["items"]["items"] == schemas["result"]
    assert schemas["collected"]["properties"]["results"]["items"] == schemas["result"]


@pytest.mark.parametrize(
    "fixture_id,items",
    [
        ("Provider-Batch", ITEMS),
        ("provider-batch", ITEMS[:-1]),
        (
            "provider-batch",
            tuple({**item, "index": 7 if item["index"] == 0 else item["index"]} for item in ITEMS),
        ),
    ],
)
def test_provision_refuses_non_immutable_identity_or_non_exact_batch(
    fixture_id: str,
    items: tuple[dict[str, object], ...],
) -> None:
    def request(_method: str, _path: str, _payload: dict[str, Any] | None) -> _Response:
        raise AssertionError("invalid fixture must fail before API mutation")

    with pytest.raises(ValueError):
        provision_provider_batch_fixture(request=request, fixture_id=fixture_id, items=items)


def test_unsealed_manifest_is_exclusive_private_staging_metadata(tmp_path: Path) -> None:
    fixture, _ = _provision()
    destination = tmp_path / "staging" / "fixture.json"

    written = write_unsealed_provider_batch_manifest(destination, fixture)
    payload = json.loads(written.read_text(encoding="utf-8"))

    assert payload["sealed"] is False
    assert payload["evidence_status"] == "staging"
    assert payload["provider_calls_performed"] == 0
    assert payload["activated"] is False
    assert payload["invoked"] is False
    assert written.stat().st_mode & 0o077 == 0
    with pytest.raises(FileExistsError):
        write_unsealed_provider_batch_manifest(destination, fixture)
