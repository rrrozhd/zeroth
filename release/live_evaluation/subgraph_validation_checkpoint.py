"""Fail-closed provider-free subgraph inspection and recovery checkpoints."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .provider_free_composed import ITEMS


def _object(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeError(f"{label} must be an object")
    return value


def _list(value: object, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise RuntimeError(f"{label} must be a list")
    return value


def _validate_restored_projection(value: object, *, label: str) -> dict[str, object]:
    projection = _object(value, label=label)
    health = _object(projection.get("health"), label=f"{label} health")
    parent = _object(projection.get("parent"), label=f"{label} parent")
    children = _list(projection.get("children"), label=f"{label} children")
    parent_identity = parent.get("identity")
    if (
        health.get("status") != "ok"
        or parent.get("status") != "succeeded"
        or not isinstance(parent_identity, str)
        or not parent_identity
        or parent.get("parent_identity") is not None
        or health.get("deployment_ref") != parent.get("deployment_ref")
        or health.get("graph_version_ref") != parent.get("graph_version_ref")
    ):
        raise RuntimeError(f"{label} serving parent projection is invalid or drifted")
    if len(children) != 8 or not all(isinstance(child, Mapping) for child in children):
        raise RuntimeError(f"{label} requires exactly eight restored children")
    identities = [child.get("identity") for child in children]
    threads = [child.get("thread_identity") for child in children]
    if (
        any(not isinstance(value, str) or not value for value in (*identities, *threads))
        or len(set(identities)) != 8
        or len(set(threads)) != 8
        or any(
            child.get("parent_identity") != parent_identity or child.get("status") != "succeeded"
            for child in children
        )
    ):
        raise RuntimeError(f"{label} child lineage is incomplete or not isolated")
    return {
        "health": dict(health),
        "parent": dict(parent),
        "children": [dict(child) for child in children],
    }


def validate_restart_proof(proof: Mapping[str, object]) -> dict[str, object]:
    """Require one real restart with an exact parent/child projection on both sides."""
    if proof.get("provider_calls_performed") != 0:
        raise RuntimeError("subgraph restart proof must remain provider-free")
    before_start = proof.get("container_started_at_before")
    after_start = proof.get("container_started_at_after")
    if (
        not isinstance(before_start, str)
        or not before_start
        or not isinstance(after_start, str)
        or not after_start
        or before_start == after_start
    ):
        raise RuntimeError("container start identity did not change across restart")
    before = _validate_restored_projection(proof.get("before"), label="pre-restart")
    after = _validate_restored_projection(proof.get("after"), label="post-restart")
    if before != after:
        raise RuntimeError("serving parent or child projection drifted across restart")
    return {
        "restored_parent_identity": after["parent"]["identity"],  # type: ignore[index]
        "restored_child_count": len(after["children"]),  # type: ignore[arg-type]
        "serving_identity": after["health"],
    }


def validate_partial_failure_summary(
    summary: Mapping[str, object],
    *,
    expected_deployment_ref: str,
    expected_graph_version_ref: str,
) -> dict[str, object]:
    """Require a successful parent with one explicit failed child at branch three."""
    if summary.get("provider_calls_performed") != 0:
        raise RuntimeError("partial-failure checkpoint must remain provider-free")
    health = _object(summary.get("health"), label="health")
    if (
        health.get("status") != "ok"
        or health.get("deployment_ref") != expected_deployment_ref
        or health.get("graph_version_ref") != expected_graph_version_ref
    ):
        raise RuntimeError("partial-failure serving identity is incorrect")
    parent = _object(summary.get("parent"), label="parent")
    parent_identity = parent.get("identity")
    if parent.get("status") != "succeeded" or not isinstance(parent_identity, str):
        raise RuntimeError("best-effort parent did not complete successfully")
    terminal_output = _object(parent.get("terminal_output"), label="parent terminal output")
    results = _list(terminal_output.get("items"), label="partial result items")
    expected_results: list[object] = [dict(item) for item in ITEMS]
    expected_results[3] = None
    if results != expected_results:
        raise RuntimeError("partial result must preserve order with one null slot at branch three")

    children = _list(summary.get("children"), label="children")
    if len(children) != 8 or not all(isinstance(child, Mapping) for child in children):
        raise RuntimeError("partial-failure checkpoint requires exactly eight child runs")
    branch_indices = [child.get("branch_index") for child in children]
    identities = [child.get("identity") for child in children]
    threads = [child.get("thread_identity") for child in children]
    if branch_indices != list(range(8)):
        raise RuntimeError("child branches are not in deterministic index order")
    if (
        any(not isinstance(value, str) or not value for value in (*identities, *threads))
        or len(set(identities)) != 8
        or len(set(threads)) != 8
        or any(child.get("parent_identity") != parent_identity for child in children)
    ):
        raise RuntimeError("child runs are not durably isolated")
    failed = [child for child in children if child.get("status") == "failed"]
    succeeded = [child for child in children if child.get("status") == "succeeded"]
    if len(failed) != 1 or len(succeeded) != 7 or failed[0].get("branch_index") != 3:
        raise RuntimeError("partial collection requires exactly one failed child at branch three")
    failed_child = _object(summary.get("failed_child"), label="failed child")
    if (
        failed_child.get("identity") != failed[0].get("identity")
        or failed_child.get("status") != "failed"
        or failed_child.get("failure_reason") != "node_execution_failed"
    ):
        raise RuntimeError("failed child status and failure reason are not correlated")

    economics = _object(summary.get("economics"), label="economics")
    if (
        economics.get("priced_call_count") != 0
        or economics.get("total_cost_usd") != 0
        or economics.get("cost_identity_state") != "not_applicable_no_priced_call"
    ):
        raise RuntimeError("provider-free partial failure must have exact zero cost")
    audit = _object(summary.get("audit"), label="audit")
    child_record_counts = _list(audit.get("child_record_counts"), label="child audit record counts")
    parent_audit_ids = _list(audit.get("parent_audit_ids"), label="parent audit identities")
    child_audit_ids = _list(audit.get("child_audit_ids"), label="child audit identities")
    parent_record_count = audit.get("parent_record_count")
    if (
        audit.get("all_records_signed") is not True
        or audit.get("chain_state") != "chain_intact_signatures_valid"
        or not isinstance(parent_record_count, int)
        or parent_record_count < 1
        or len(child_record_counts) != 8
        or any(not isinstance(count, int) or count < 1 for count in child_record_counts)
    ):
        raise RuntimeError("partial collection lacks complete signed audit evidence")
    all_audit_ids = [*parent_audit_ids]
    if (
        len(parent_audit_ids) != parent_record_count
        or len(child_audit_ids) != 8
        or any(not isinstance(ids, list) for ids in child_audit_ids)
        or any(
            len(ids) != count
            for ids, count in zip(child_audit_ids, child_record_counts, strict=True)
        )
    ):
        raise RuntimeError("partial collection lacks exact audit identities")
    for ids in child_audit_ids:
        all_audit_ids.extend(ids)
    if any(not isinstance(audit_id, str) or not audit_id for audit_id in all_audit_ids) or len(
        set(all_audit_ids)
    ) != len(all_audit_ids):
        raise RuntimeError("partial collection lacks exact audit identities")
    economics_identities = _object(
        summary.get("economics_identities"), label="economics identities"
    )
    if economics_identities != {
        "cost_event_ids": [],
        "provider_request_ids": [],
        "operation_ids": [],
        "state": "not_applicable_no_priced_call",
    }:
        raise RuntimeError("provider-free partial collection has unexpected economics identities")
    return {
        "parent_identity": parent_identity,
        "failed_branch_index": 3,
        "successful_child_count": 7,
        "failed_child_identity": failed_child["identity"],
    }
