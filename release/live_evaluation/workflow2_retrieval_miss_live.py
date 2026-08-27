"""Unsealed provider-independent Workflow-2 retrieval-miss fixture and validator.

The exact-eight parent fans out with concurrency four. Seven branches take a
local executable success path. Branch three alone reaches the run-scoped
``ephemeral`` retrieval connector, whose namespace is empty for a new child
run, then fails at a deterministic assertion node so the parent can exercise
best-effort partial collection. No provider-capable node or connector is
reachable.

Provisioning uses only public Studio/deployment APIs. This module deliberately
does not restart a service or seal evidence; those are later, validated steps.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .provider_free_composed import Request, _object, _post, _publish_deploy_workflow

RETRIEVAL_ITEMS = tuple(
    {
        "index": index,
        "value": f"deterministic-item-{index}",
        "query": f"provider-free-workflow2-retrieval-{index}",
    }
    for index in range(8)
)


@dataclass(frozen=True, slots=True)
class ProviderFreeWorkflow2RetrievalMissFixture:
    schema_version: int
    fixture_id: str
    child_workflow_id: str
    child_graph_version_ref: str
    child_deployment_ref: str
    child_deployment_version: int
    parent_workflow_id: str
    parent_graph_version_ref: str
    parent_deployment_ref: str
    parent_deployment_version: int
    items: tuple[dict[str, object], ...] = RETRIEVAL_ITEMS
    retrieval_miss_branch_index: int = 3
    max_concurrency: int = 4
    provider_calls_performed: int = 0
    provider_economics_status: str = "not_applicable_no_priced_call"
    restart_required: bool = True


def _node(
    node_id: str,
    node_type: str,
    *,
    label: str,
    x: int,
    y: int = 0,
    config: Mapping[str, object] | None = None,
    input_contract_ref: str,
    output_contract_ref: str,
    capability_bindings: Sequence[str] = (),
    parallel_config: Mapping[str, object] | None = None,
) -> dict[str, object]:
    data: dict[str, object] = {
        "label": label,
        "config": dict(config or {}),
        "input_contract_ref": input_contract_ref,
        "output_contract_ref": output_contract_ref,
    }
    if capability_bindings:
        data["capability_bindings"] = list(capability_bindings)
    if parallel_config is not None:
        data["parallel_config"] = dict(parallel_config)
    return {
        "id": node_id,
        "type": node_type,
        "position": {"x": x, "y": y},
        "data": data,
    }


def _create_workflow(request: Request, *, name: str) -> str:
    created = _post(
        request,
        "/api/studio/v1/workflows",
        {"name": name},
        expected=201,
        label=f"create {name}",
    )
    workflow_id = created.get("id")
    if not isinstance(workflow_id, str) or not workflow_id:
        raise RuntimeError(f"workflow identity is missing for {name}")
    return workflow_id


def _save(
    request: Request,
    *,
    workflow_id: str,
    nodes: list[dict[str, object]],
    edges: list[dict[str, object]],
    max_steps: int,
) -> None:
    _object(
        request(
            "PUT",
            f"/api/studio/v1/workflows/{workflow_id}",
            {
                "entry_step": nodes[0]["id"],
                "nodes": nodes,
                "edges": edges,
                "execution_settings": {
                    "max_total_steps": max_steps,
                    "max_total_runtime_seconds": 90,
                    "max_visits_per_node": 8,
                    "default_timeout_seconds": 30,
                },
            },
        ),
        expected=200,
        label=f"save {workflow_id}",
    )


def provision_workflow2_retrieval_miss_fixture(
    *, request: Request, fixture_id: str
) -> ProviderFreeWorkflow2RetrievalMissFixture:
    """Publish an exact-eight, concurrency-four, local retrieval-miss fixture."""
    if re.fullmatch(r"[a-z0-9][a-z0-9-]{2,47}", fixture_id) is None:
        raise ValueError("fixture_id must be a short lowercase slug")

    contract_prefix = f"contract://provider-free-w2-retrieval-miss-{fixture_id}"
    item_contract = f"{contract_prefix}.item"
    batch_contract = f"{contract_prefix}.batch"
    item_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["index", "value", "query"],
        "properties": {
            "index": {"type": "integer", "minimum": 0, "maximum": 7},
            "value": {"type": "string", "pattern": "^deterministic-item-[0-7]$"},
            "query": {
                "type": "string",
                "pattern": "^provider-free-workflow2-retrieval-[0-7]$",
            },
            "retrieved": {"type": "array"},
            "zeroth_if": {"type": "object"},
        },
    }
    batch_item_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["index", "value", "query"],
        "properties": {
            "index": {"type": "integer", "minimum": 0, "maximum": 7},
            "value": {"type": "string", "pattern": "^deterministic-item-[0-7]$"},
            "query": {
                "type": "string",
                "pattern": "^provider-free-workflow2-retrieval-[0-7]$",
            },
        },
    }
    batch_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["items"],
        "properties": {
            "items": {
                "type": "array",
                "minItems": 8,
                "maxItems": 8,
                "items": batch_item_schema,
            }
        },
    }
    for name, schema in ((item_contract, item_schema), (batch_contract, batch_schema)):
        contract = _post(
            request,
            "/api/studio/v1/contracts",
            {
                "name": name,
                "json_schema": schema,
                "metadata": {
                    "campaign_slice": "workflow2-retrieval-miss-live",
                    "provider_calls_performed": 0,
                    "connector_boundary": "run-scoped-ephemeral",
                },
            },
            expected=201,
            label=f"create {name}",
        )
        if contract.get("name") != name or contract.get("version") != 1:
            raise RuntimeError(f"contract {name} is not a new immutable v1 fixture")

    child_deployment = f"provider-free-w2-retrieval-miss-{fixture_id}-child"
    child_id = _create_workflow(
        request,
        name=f"Workflow 2 deterministic retrieval child {fixture_id}",
    )
    success_source = "\n".join(
        (
            "import json",
            "import sys",
            "payload = json.load(sys.stdin)",
            "payload.pop('zeroth_if', None)",
            "payload.pop('retrieved', None)",
            "json.dump(payload, sys.stdout, sort_keys=True)",
        )
    )
    require_hit_source = "\n".join(
        (
            "import json",
            "import sys",
            "payload = json.load(sys.stdin)",
            "if payload.get('retrieved'):",
            "    raise RuntimeError('unexpected retrieval hit in isolated fixture')",
            "raise RuntimeError('deterministic retrieval miss')",
        )
    )
    child_nodes = [
        _node(
            "child-entry",
            "entrypoint",
            label="Investigation item",
            x=0,
            input_contract_ref=item_contract,
            output_contract_ref=item_contract,
        ),
        _node(
            "retrieval-decision",
            "if",
            label="Exercise retrieval miss?",
            x=240,
            config={"expression": "payload.index == 3"},
            input_contract_ref=item_contract,
            output_contract_ref=item_contract,
        ),
        _node(
            "local-retrieval",
            "retrieval",
            label="Run-scoped local retrieval",
            x=500,
            y=-120,
            config={
                "connector_ref": "ephemeral",
                "query_key": "query",
                "top_k": 1,
                "scope": "run",
                "as_name": "retrieved",
            },
            input_contract_ref=item_contract,
            output_contract_ref=item_contract,
            capability_bindings=("memory_read",),
        ),
        _node(
            "require-retrieval-hit",
            "code",
            label="Require grounded result",
            x=760,
            y=-120,
            config={
                "execution_mode": "inline",
                "inline_source": require_hit_source,
                "timeout_seconds": 5,
                "output_extraction_strategy": "json_stdout",
            },
            input_contract_ref=item_contract,
            output_contract_ref=item_contract,
        ),
        _node(
            "complete-sibling",
            "code",
            label="Complete local sibling",
            x=500,
            y=120,
            config={
                "execution_mode": "inline",
                "inline_source": success_source,
                "timeout_seconds": 5,
                "output_extraction_strategy": "json_stdout",
            },
            input_contract_ref=item_contract,
            output_contract_ref=item_contract,
        ),
    ]
    child_edges = [
        {"id": "entry-decision", "source": "child-entry", "target": "retrieval-decision"},
        {
            "id": "decision-retrieval",
            "source": "retrieval-decision",
            "target": "local-retrieval",
            "source_handle": "true",
        },
        {
            "id": "decision-success",
            "source": "retrieval-decision",
            "target": "complete-sibling",
            "source_handle": "false",
        },
        {
            "id": "retrieval-require-hit",
            "source": "local-retrieval",
            "target": "require-retrieval-hit",
        },
    ]
    _save(
        request,
        workflow_id=child_id,
        nodes=child_nodes,
        edges=child_edges,
        max_steps=8,
    )
    child_graph, child_version = _publish_deploy_workflow(
        request=request,
        workflow_id=child_id,
        deployment_ref=child_deployment,
    )

    parent_deployment = f"provider-free-w2-retrieval-miss-{fixture_id}-parent"
    parent_id = _create_workflow(
        request,
        name=f"Workflow 2 exact-eight retrieval miss {fixture_id}",
    )
    parent_nodes = [
        _node(
            "batch-input",
            "entrypoint",
            label="Eight retrieval investigations",
            x=0,
            input_contract_ref=batch_contract,
            output_contract_ref=batch_contract,
            parallel_config={
                "split_path": "items",
                "merge_strategy": "collect",
                "fail_mode": "best_effort",
                "max_branches": 8,
                "max_concurrency": 4,
                "batch_size": 8,
                "branch_timeout_seconds": 30,
            },
        ),
        _node(
            "retrieval-child",
            "subgraph",
            label="Investigate with local retrieval",
            x=360,
            config={
                "graph_ref": child_deployment,
                "version": child_version,
                "thread_participation": "isolated",
                "max_depth": 1,
            },
            input_contract_ref=item_contract,
            output_contract_ref=item_contract,
        ),
    ]
    _save(
        request,
        workflow_id=parent_id,
        nodes=parent_nodes,
        edges=[{"id": "batch-child", "source": "batch-input", "target": "retrieval-child"}],
        max_steps=24,
    )
    parent_graph, parent_version = _publish_deploy_workflow(
        request=request,
        workflow_id=parent_id,
        deployment_ref=parent_deployment,
    )
    return ProviderFreeWorkflow2RetrievalMissFixture(
        schema_version=1,
        fixture_id=fixture_id,
        child_workflow_id=child_id,
        child_graph_version_ref=child_graph,
        child_deployment_ref=child_deployment,
        child_deployment_version=child_version,
        parent_workflow_id=parent_id,
        parent_graph_version_ref=parent_graph,
        parent_deployment_ref=parent_deployment,
        parent_deployment_version=parent_version,
    )


def _object_value(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeError(f"{label} must be an object")
    return value


def _string_list(value: object, *, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise RuntimeError(f"{label} must be a list of identities")
    return value


def validate_workflow2_retrieval_miss_summary(
    summary: Mapping[str, object],
    *,
    expected_deployment_ref: str,
    expected_graph_version_ref: str,
    expected_child_deployment_ref: str,
    expected_child_deployment_version: int,
) -> dict[str, object]:
    """Validate a live, unsealed exact-eight retrieval-miss observation."""
    if (
        summary.get("schema_version") != 1
        or summary.get("configured_max_concurrency") != 4
        or summary.get("retrieval_miss_branch_index") != 3
    ):
        raise RuntimeError("Workflow 2 retrieval-miss summary identity is invalid")
    provider_ids = _string_list(
        summary.get("provider_request_ids"), label="provider request identities"
    )
    cost_ids = _string_list(summary.get("cost_event_ids"), label="cost event identities")
    if (
        summary.get("provider_calls_performed") != 0
        or provider_ids
        or cost_ids
        or summary.get("priced_call_count") != 0
    ):
        raise RuntimeError("provider-independent retrieval miss contains provider activity")
    total_cost = summary.get("total_cost_usd")
    if (
        not isinstance(total_cost, (int, float))
        or isinstance(total_cost, bool)
        or float(total_cost) != 0.0
    ):
        raise RuntimeError("provider-independent retrieval miss contains nonzero cost")

    health = _object_value(summary.get("health"), label="health")
    if (
        health.get("status") != "ok"
        or health.get("deployment_ref") != expected_deployment_ref
        or health.get("graph_version_ref") != expected_graph_version_ref
    ):
        raise RuntimeError("Workflow 2 retrieval-miss serving identity drifted")
    parent = _object_value(summary.get("parent"), label="parent")
    parent_id = parent.get("run_id")
    if not isinstance(parent_id, str) or not parent_id or parent.get("status") != "succeeded":
        raise RuntimeError("best-effort retrieval-miss parent did not succeed")
    terminal = _object_value(parent.get("terminal_output"), label="parent terminal output")
    items = terminal.get("items")
    expected_items: list[object] = [dict(item) for item in RETRIEVAL_ITEMS]
    expected_items[3] = None
    if items != expected_items:
        raise RuntimeError("retrieval-miss partial collection is not exact or ordered")

    raw_children = summary.get("children")
    if (
        not isinstance(raw_children, list)
        or len(raw_children) != 8
        or any(not isinstance(child, Mapping) for child in raw_children)
    ):
        raise RuntimeError("retrieval-miss summary requires exactly eight child runs")
    children = list(raw_children)
    if [child.get("branch_index") for child in children] != list(range(8)):
        raise RuntimeError("retrieval-miss child branches are not in exact order")
    child_ids = [child.get("run_id") for child in children]
    thread_ids = [child.get("thread_id") for child in children]
    if (
        any(not isinstance(value, str) or not value for value in (*child_ids, *thread_ids))
        or len(set(child_ids)) != 8
        or len(set(thread_ids)) != 8
        or any(child.get("parent_run_id") != parent_id for child in children)
    ):
        raise RuntimeError("retrieval-miss parent/child lineage is incomplete")
    succeeded = [child for child in children if child.get("status") == "succeeded"]
    failed = [child for child in children if child.get("status") == "failed"]
    if (
        len(succeeded) != 7
        or len(failed) != 1
        or failed[0].get("branch_index") != 3
        or failed[0].get("failure_reason") != "node_execution_failed"
    ):
        raise RuntimeError("retrieval miss requires exactly seven successes and one failure")

    miss = _object_value(summary.get("retrieval_miss"), label="retrieval miss")
    retrieval_node_id = (
        f"branch:3:subgraph:{expected_child_deployment_ref}:"
        f"{expected_child_deployment_version}:local-retrieval"
    )
    failure_node_id = (
        f"branch:3:subgraph:{expected_child_deployment_ref}:"
        f"{expected_child_deployment_version}:require-retrieval-hit"
    )
    if (
        miss.get("child_run_id") != failed[0].get("run_id")
        or miss.get("retrieval_node_id") != retrieval_node_id
        or miss.get("retrieval_result_count") != 0
        or miss.get("failure_node_id") != failure_node_id
        or miss.get("failure_reason") != "node_execution_failed"
    ):
        raise RuntimeError("retrieval miss lacks an exact zero-result retrieval proof")

    refresh = _object_value(summary.get("refresh"), label="refresh")
    before_children = _string_list(
        refresh.get("before_child_run_ids"), label="pre-refresh child identities"
    )
    restored_children = _string_list(
        refresh.get("restored_child_run_ids"), label="restored child identities"
    )
    if (
        refresh.get("before_parent_run_id") != parent_id
        or refresh.get("restored_parent_run_id") != parent_id
        or before_children != restored_children
        or len(before_children) != len(child_ids)
        or set(before_children) != set(child_ids)
    ):
        raise RuntimeError("retrieval-miss UI refresh did not restore exact lineage")

    audit = _object_value(summary.get("audit"), label="audit")
    links = audit.get("child_parent_links")
    if (
        audit.get("signed_parent_chain") is not True
        or audit.get("signed_child_chain_count") != 8
        or audit.get("unsigned_record_count") != 0
        or audit.get("parent_run_id") != parent_id
        or not isinstance(links, list)
        or len(links) != 8
        or any(
            not isinstance(link, Mapping)
            or link.get("child_run_id") != child_ids[index]
            or link.get("parent_run_id") != parent_id
            for index, link in enumerate(links)
        )
    ):
        raise RuntimeError("retrieval miss lacks signed parent/child audit linkage")

    return {
        "parent_run_id": parent_id,
        "child_run_ids": child_ids,
        "successful_child_count": 7,
        "failed_child_count": 1,
        "retrieval_miss_branch_index": 3,
        "priced_call_count": 0,
        "total_cost_usd": 0.0,
        "provider_request_ids": provider_ids,
        "cost_event_ids": cost_ids,
        "refresh_restored": True,
    }
