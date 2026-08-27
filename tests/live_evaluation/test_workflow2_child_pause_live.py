from __future__ import annotations

from typing import Any

import pytest

from release.live_evaluation.provider_free_composed import ITEMS
from release.live_evaluation.workflow2_child_pause_live import (
    ProviderFreeWorkflow2ChildPauseFixture,
    provision_workflow2_child_pause_fixture,
    validate_workflow2_child_pause_summary,
)


class _Response:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self) -> object:
        return self._payload


def test_fixture_uses_visible_if_and_exact_eight_item_parallel_child_pause() -> None:
    calls: list[tuple[str, str, dict[str, Any] | None]] = []
    workflow_ids = iter(("pause-child", "pause-parent"))

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

    fixture = provision_workflow2_child_pause_fixture(
        request=request,
        fixture_id="w2-pause-live",
    )

    assert isinstance(fixture, ProviderFreeWorkflow2ChildPauseFixture)
    assert fixture.items == ITEMS
    assert fixture.approval_branch_index == 7
    assert fixture.max_concurrency == 4
    assert fixture.provider_calls_performed == 0
    saves = [payload for method, path, payload in calls if method == "PUT"]
    assert len(saves) == 2
    child, parent = saves
    assert [node["type"] for node in child["nodes"]] == [
        "entrypoint",
        "if",
        "code",
        "human_approval",
        "code",
        "code",
    ]
    decision = child["nodes"][1]
    assert decision["data"]["config"] == {"expression": "payload.index == 7"}
    routes = {
        edge["source_handle"]: edge["target"]
        for edge in child["edges"]
        if edge["source"] == "pause-decision"
    }
    assert routes == {"true": "approval-delay", "false": "complete-sibling"}
    assert "time.sleep(1.5)" in child["nodes"][2]["data"]["config"]["inline_source"]
    assert "payload.pop('zeroth_if', None)" in child["nodes"][4]["data"]["config"][
        "inline_source"
    ]
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
    assert all(
        node["type"] not in {"agent", "retrieval"}
        for save in saves
        for node in save["nodes"]
    )


def _outcome(decision: str) -> dict[str, object]:
    parent = f"parent-{decision}"
    before = [
        {
            "run_id": f"{decision}-child-{index}",
            "thread_id": f"{decision}-thread-{index}",
            "parent_run_id": parent,
            "branch_index": index,
            "status": "paused_for_approval" if index == 7 else "succeeded",
        }
        for index in range(8)
    ]
    after = [
        {
            **child,
            "status": (
                "succeeded"
                if child["branch_index"] != 7 or decision == "approve"
                else "failed"
            ),
        }
        for child in before
    ]
    return {
        "decision": decision,
        "reason": f"reviewer {decision} reason",
        "parent_run_id": parent,
        "approval_id": f"approval-{decision}",
        "approval_child_run_id": f"{decision}-child-7",
        "parent_status": "succeeded" if decision == "approve" else "failed",
        "parent_failure_reason": None if decision == "approve" else "parallel_execution_failed",
        "terminal_output": {"items": list(ITEMS)} if decision == "approve" else None,
        "children_before": before,
        "children_after": after,
        "refresh_restored_parent_run_id": parent,
        "refresh_restored_approval_id": f"approval-{decision}",
        "signed_parent_chain": True,
        "signed_child_chain_count": 8,
        "continuation_audit_count": 1,
        "priced_call_count": 0,
        "total_cost_usd": 0,
    }


def test_summary_requires_two_exact_eight_child_outcomes_without_sibling_replay() -> None:
    summary = {
        "schema_version": 1,
        "provider_calls_performed": 0,
        "provider_economics_status": "blocked",
        "configured_max_concurrency": 4,
        "approval_branch_index": 7,
        "outcomes": [_outcome("approve"), _outcome("reject")],
    }

    result = validate_workflow2_child_pause_summary(summary)

    assert result == {
        "parent_run_ids": ["parent-approve", "parent-reject"],
        "completed_sibling_count": 14,
        "sibling_replay_count": 0,
        "priced_call_count": 0,
        "total_cost_usd": 0.0,
        "provider_economics_status": "blocked",
    }


def test_summary_rejects_cancelled_sibling_or_relabelled_refresh() -> None:
    cancelled = _outcome("approve")
    cancelled["children_before"][3]["status"] = "failed"  # type: ignore[index]
    with pytest.raises(RuntimeError, match="seven completed siblings"):
        validate_workflow2_child_pause_summary(
            {
                "schema_version": 1,
                "provider_calls_performed": 0,
                "provider_economics_status": "blocked",
                "configured_max_concurrency": 4,
                "approval_branch_index": 7,
                "outcomes": [cancelled, _outcome("reject")],
            }
        )

    relabelled = _outcome("approve")
    relabelled["refresh_restored_parent_run_id"] = "different-parent"
    with pytest.raises(RuntimeError, match="refresh"):
        validate_workflow2_child_pause_summary(
            {
                "schema_version": 1,
                "provider_calls_performed": 0,
                "provider_economics_status": "blocked",
                "configured_max_concurrency": 4,
                "approval_branch_index": 7,
                "outcomes": [relabelled, _outcome("reject")],
            }
        )
