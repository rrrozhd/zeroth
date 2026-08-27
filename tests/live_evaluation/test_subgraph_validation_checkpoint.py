from __future__ import annotations

from copy import deepcopy

import pytest

from release.live_evaluation.subgraph_validation_checkpoint import (
    validate_partial_failure_summary,
    validate_restart_proof,
)


def _restart_proof() -> dict[str, object]:
    health = {
        "status": "ok",
        "campaign_id": "evaluation-studio-v1",
        "deployment_ref": "provider-free-composed-parent",
        "deployment_version": 1,
        "graph_version_ref": "parent@1",
    }
    parent = {
        "identity": "parent-run",
        "status": "succeeded",
        "deployment_ref": "provider-free-composed-parent",
        "graph_version_ref": "parent@1",
        "thread_identity": "parent-thread",
        "parent_identity": None,
    }
    children = [
        {
            "identity": f"child-{index}",
            "status": "succeeded",
            "deployment_ref": "provider-free-composed-child",
            "graph_version_ref": "child@1",
            "thread_identity": f"child-thread-{index}",
            "parent_identity": "parent-run",
        }
        for index in range(8)
    ]
    return {
        "container_started_at_before": "2026-08-25T23:00:00Z",
        "container_started_at_after": "2026-08-25T23:01:00Z",
        "before": deepcopy({"health": health, "parent": parent, "children": children}),
        "after": deepcopy({"health": health, "parent": parent, "children": children}),
        "provider_calls_performed": 0,
    }


def _partial_failure_summary() -> dict[str, object]:
    children = [
        {
            "identity": f"child-{index}",
            "thread_identity": f"thread-{index}",
            "parent_identity": "parent-run",
            "status": "failed" if index == 3 else "succeeded",
            "branch_index": index,
        }
        for index in range(8)
    ]
    return {
        "health": {
            "status": "ok",
            "deployment_ref": "provider-free-partial-parent",
            "graph_version_ref": "partial-parent@1",
        },
        "parent": {
            "identity": "parent-run",
            "status": "succeeded",
            "terminal_output": {
                "items": [
                    {"index": 0, "value": "deterministic-item-0"},
                    {"index": 1, "value": "deterministic-item-1"},
                    {"index": 2, "value": "deterministic-item-2"},
                    None,
                    {"index": 4, "value": "deterministic-item-4"},
                    {"index": 5, "value": "deterministic-item-5"},
                    {"index": 6, "value": "deterministic-item-6"},
                    {"index": 7, "value": "deterministic-item-7"},
                ]
            },
        },
        "children": children,
        "failed_child": {
            "identity": "child-3",
            "status": "failed",
            "failure_reason": "node_execution_failed",
        },
        "economics": {
            "priced_call_count": 0,
            "total_cost_usd": 0,
            "cost_identity_state": "not_applicable_no_priced_call",
        },
        "audit": {
            "parent_record_count": 9,
            "parent_audit_ids": [f"parent-audit-{index}" for index in range(9)],
            "child_record_counts": [2, 2, 2, 1, 2, 2, 2, 2],
            "child_audit_ids": [
                [f"child-{index}-audit-{audit}" for audit in range(count)]
                for index, count in enumerate([2, 2, 2, 1, 2, 2, 2, 2])
            ],
            "all_records_signed": True,
            "chain_state": "chain_intact_signatures_valid",
        },
        "economics_identities": {
            "cost_event_ids": [],
            "provider_request_ids": [],
            "operation_ids": [],
            "state": "not_applicable_no_priced_call",
        },
        "provider_calls_performed": 0,
    }


def test_restart_proof_requires_exact_parent_and_child_restoration() -> None:
    validated = validate_restart_proof(_restart_proof())

    assert validated["restored_parent_identity"] == "parent-run"
    assert validated["restored_child_count"] == 8


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("container_started_at_after",), "2026-08-25T23:00:00Z", "start identity"),
        (("after", "health", "graph_version_ref"), "other@1", "drifted"),
        (("after", "children", 7, "identity"), "other-child", "drifted"),
        (("provider_calls_performed",), 1, "provider-free"),
    ],
)
def test_restart_proof_fails_closed_on_weak_evidence(
    path: tuple[str | int, ...], value: object, message: str
) -> None:
    proof = deepcopy(_restart_proof())
    target: object = proof
    for segment in path[:-1]:
        target = target[segment]  # type: ignore[index]
    target[path[-1]] = value  # type: ignore[index]

    with pytest.raises(RuntimeError, match=message):
        validate_restart_proof(proof)


def test_partial_failure_requires_one_failed_child_and_ordered_partial_result() -> None:
    validated = validate_partial_failure_summary(
        _partial_failure_summary(),
        expected_deployment_ref="provider-free-partial-parent",
        expected_graph_version_ref="partial-parent@1",
    )

    assert validated["parent_identity"] == "parent-run"
    assert validated["failed_branch_index"] == 3
    assert validated["successful_child_count"] == 7


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("parent", "terminal_output", "items", 3), {"index": 3}, "null slot"),
        (("children", 4, "status"), "failed", "exactly one"),
        (("children", 6, "thread_identity"), "thread-5", "isolated"),
        (("economics", "total_cost_usd"), 0.01, "zero cost"),
        (("audit", "all_records_signed"), False, "signed audit"),
        (("audit", "parent_audit_ids"), [], "audit identities"),
        (("economics_identities", "cost_event_ids"), ["unexpected"], "economics identities"),
    ],
)
def test_partial_failure_fails_closed_on_relabelled_or_incomplete_evidence(
    path: tuple[str | int, ...], value: object, message: str
) -> None:
    summary = deepcopy(_partial_failure_summary())
    target: object = summary
    for segment in path[:-1]:
        target = target[segment]  # type: ignore[index]
    target[path[-1]] = value  # type: ignore[index]

    with pytest.raises(RuntimeError, match=message):
        validate_partial_failure_summary(
            summary,
            expected_deployment_ref="provider-free-partial-parent",
            expected_graph_version_ref="partial-parent@1",
        )
