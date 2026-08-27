"""Provider-free exact-eight Workflow 2 child-pause fixture and validators.

The child graph uses a visible If control node. Branches zero through six take
the immediate completion route; branch seven takes a bounded delay before its
child-owned approval. With parent concurrency four, the delay gives all seven
siblings time to settle before the one approval pause is published. No agent,
retrieval connector, provider secret, or external action is reachable.

This module provisions immutable fixtures and validates unsealed browser
observations. It does not restart services, seal evidence, or update ledgers.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from .provider_free_composed import (
    ITEMS,
    Request,
    _object,
    _post,
    _publish_deploy_workflow,
)


@dataclass(frozen=True, slots=True)
class ProviderFreeWorkflow2ChildPauseFixture:
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
    items: tuple[dict[str, object], ...] = ITEMS
    approval_branch_index: int = 7
    max_concurrency: int = 4
    provider_calls_performed: int = 0
    provider_economics_status: str = "blocked"


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
    parallel_config: Mapping[str, object] | None = None,
) -> dict[str, object]:
    data: dict[str, object] = {
        "label": label,
        "config": dict(config or {}),
        "input_contract_ref": input_contract_ref,
        "output_contract_ref": output_contract_ref,
    }
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


def provision_workflow2_child_pause_fixture(
    *, request: Request, fixture_id: str
) -> ProviderFreeWorkflow2ChildPauseFixture:
    """Publish one exact-eight, concurrency-four child approval fixture."""
    if re.fullmatch(r"[a-z0-9][a-z0-9-]{2,47}", fixture_id) is None:
        raise ValueError("fixture_id must be a short lowercase slug")

    contract_prefix = f"contract://provider-free-w2-child-pause-{fixture_id}"
    item_contract = f"{contract_prefix}.item"
    batch_contract = f"{contract_prefix}.batch"
    item_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["index", "value"],
        "properties": {
            "index": {"type": "integer", "minimum": 0, "maximum": 7},
            "value": {"type": "string", "pattern": "^deterministic-item-[0-7]$"},
            # The If controller carries its route in this internal envelope.
            # Both terminal code paths remove it before the child returns.
            "zeroth_if": {"type": "object"},
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
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["index", "value"],
                    "properties": {
                        "index": {"type": "integer", "minimum": 0, "maximum": 7},
                        "value": {
                            "type": "string",
                            "pattern": "^deterministic-item-[0-7]$",
                        },
                    },
                },
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
                    "campaign_slice": "workflow2-child-pause-live",
                    "provider_calls_performed": 0,
                },
            },
            expected=201,
            label=f"create {name}",
        )
        if contract.get("name") != name or contract.get("version") != 1:
            raise RuntimeError(f"contract {name} is not a new immutable v1 fixture")

    child_deployment = f"provider-free-w2-child-pause-{fixture_id}-child"
    child_id = _create_workflow(
        request,
        name=f"Workflow 2 exact-eight approval child {fixture_id}",
    )
    echo_source = "\n".join(
        (
            "import json",
            "import sys",
            "payload = json.load(sys.stdin)",
            "payload.pop('zeroth_if', None)",
            "json.dump(payload, sys.stdout, sort_keys=True)",
        )
    )
    delay_source = "\n".join(
        (
            "import json",
            "import sys",
            "import time",
            "payload = json.load(sys.stdin)",
            "time.sleep(1.5)",
            "json.dump(payload, sys.stdout, sort_keys=True)",
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
            "pause-decision",
            "if",
            label="Needs reviewer pause?",
            x=240,
            config={"expression": "payload.index == 7"},
            input_contract_ref=item_contract,
            output_contract_ref=item_contract,
        ),
        _node(
            "approval-delay",
            "code",
            label="Let siblings settle",
            x=500,
            y=-120,
            config={
                "execution_mode": "inline",
                "inline_source": delay_source,
                "timeout_seconds": 5,
                "output_extraction_strategy": "json_stdout",
            },
            input_contract_ref=item_contract,
            output_contract_ref=item_contract,
        ),
        _node(
            "review-child",
            "human_approval",
            label="Review final child",
            x=760,
            y=-120,
            config={
                "approval_payload_schema_ref": item_contract,
                "resolution_schema_ref": item_contract,
                "approval_policy_config": {
                    "allow_edits": False,
                    "require_explicit_decision": True,
                },
                "pause_behavior_config": {"persist_before_pause": True},
                "sla_timeout_seconds": 300,
            },
            input_contract_ref=item_contract,
            output_contract_ref=item_contract,
        ),
        _node(
            "approved-output",
            "code",
            label="Approved child result",
            x=1020,
            y=-120,
            config={
                "execution_mode": "inline",
                "inline_source": echo_source,
                "timeout_seconds": 5,
                "output_extraction_strategy": "json_stdout",
            },
            input_contract_ref=item_contract,
            output_contract_ref=item_contract,
        ),
        _node(
            "complete-sibling",
            "code",
            label="Complete sibling",
            x=500,
            y=120,
            config={
                "execution_mode": "inline",
                "inline_source": echo_source,
                "timeout_seconds": 5,
                "output_extraction_strategy": "json_stdout",
            },
            input_contract_ref=item_contract,
            output_contract_ref=item_contract,
        ),
    ]
    child_edges = [
        {"id": "entry-decision", "source": "child-entry", "target": "pause-decision"},
        {
            "id": "decision-approval",
            "source": "pause-decision",
            "target": "approval-delay",
            "source_handle": "true",
        },
        {
            "id": "decision-sibling",
            "source": "pause-decision",
            "target": "complete-sibling",
            "source_handle": "false",
        },
        {
            "id": "delay-review",
            "source": "approval-delay",
            "target": "review-child",
        },
        {
            "id": "review-output",
            "source": "review-child",
            "target": "approved-output",
        },
    ]
    _save(
        request,
        workflow_id=child_id,
        nodes=child_nodes,
        edges=child_edges,
        max_steps=10,
    )
    child_graph, child_version = _publish_deploy_workflow(
        request=request,
        workflow_id=child_id,
        deployment_ref=child_deployment,
    )

    parent_deployment = f"provider-free-w2-child-pause-{fixture_id}-parent"
    parent_id = _create_workflow(
        request,
        name=f"Workflow 2 exact-eight child pause {fixture_id}",
    )
    parent_nodes = [
        _node(
            "batch-input",
            "entrypoint",
            label="Eight investigation items",
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
            "approval-capable-child",
            "subgraph",
            label="Investigate with final review",
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
        edges=[
            {
                "id": "batch-child",
                "source": "batch-input",
                "target": "approval-capable-child",
            }
        ],
        max_steps=24,
    )
    parent_graph, parent_version = _publish_deploy_workflow(
        request=request,
        workflow_id=parent_id,
        deployment_ref=parent_deployment,
    )
    return ProviderFreeWorkflow2ChildPauseFixture(
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


def _children(value: object, *, label: str) -> list[Mapping[str, object]]:
    if not isinstance(value, list) or len(value) != 8 or not all(
        isinstance(child, Mapping) for child in value
    ):
        raise RuntimeError(f"{label} requires exactly eight child records")
    children = list(value)
    indices = [child.get("branch_index") for child in children]
    if indices != list(range(8)):
        raise RuntimeError(f"{label} is not in exact branch order")
    ids = [child.get("run_id") for child in children]
    threads = [child.get("thread_id") for child in children]
    if (
        any(not isinstance(value, str) or not value for value in (*ids, *threads))
        or len(set(ids)) != 8
        or len(set(threads)) != 8
    ):
        raise RuntimeError(f"{label} child run or thread isolation is invalid")
    return children


def validate_workflow2_child_pause_summary(summary: Mapping[str, object]) -> dict[str, object]:
    """Require one approve and reject with seven completed siblings each."""
    if (
        summary.get("schema_version") != 1
        or summary.get("provider_calls_performed") != 0
        or summary.get("provider_economics_status") != "blocked"
        or summary.get("configured_max_concurrency") != 4
        or summary.get("approval_branch_index") != 7
    ):
        raise RuntimeError("Workflow 2 child-pause summary identity is invalid")
    outcomes = summary.get("outcomes")
    if not isinstance(outcomes, list) or len(outcomes) != 2:
        raise RuntimeError("Workflow 2 child-pause summary requires approve and reject")
    expected_parent = {"approve": "succeeded", "reject": "failed"}
    seen: set[str] = set()
    parent_ids: list[str] = []
    completed_siblings = 0
    total_cost = 0.0
    for raw in outcomes:
        if not isinstance(raw, Mapping):
            raise RuntimeError("Workflow 2 child-pause outcome is malformed")
        decision = raw.get("decision")
        if decision not in expected_parent or decision in seen:
            raise RuntimeError("Workflow 2 child-pause decisions must be approve and reject")
        seen.add(str(decision))
        parent_id = raw.get("parent_run_id")
        approval_id = raw.get("approval_id")
        approval_child_id = raw.get("approval_child_run_id")
        reason = raw.get("reason")
        if not all(
            isinstance(value, str) and value
            for value in (parent_id, approval_id, approval_child_id, reason)
        ):
            raise RuntimeError("Workflow 2 child-pause outcome lacks durable identities")
        if (
            raw.get("parent_status") != expected_parent[str(decision)]
            or raw.get("refresh_restored_parent_run_id") != parent_id
            or raw.get("refresh_restored_approval_id") != approval_id
        ):
            raise RuntimeError("Workflow 2 child-pause refresh or parent verdict drifted")
        before = _children(raw.get("children_before"), label=f"{decision} children before")
        after = _children(raw.get("children_after"), label=f"{decision} children after")
        if [child["run_id"] for child in before] != [child["run_id"] for child in after]:
            raise RuntimeError("Workflow 2 child-pause sibling replay changed child identities")
        if any(child.get("parent_run_id") != parent_id for child in (*before, *after)):
            raise RuntimeError("Workflow 2 child-pause parent/child lineage drifted")
        before_succeeded = [child for child in before if child.get("status") == "succeeded"]
        before_paused = [
            child for child in before if child.get("status") == "paused_for_approval"
        ]
        if (
            len(before_succeeded) != 7
            or len(before_paused) != 1
            or before_paused[0].get("branch_index") != 7
            or before_paused[0].get("run_id") != approval_child_id
        ):
            raise RuntimeError("Workflow 2 child-pause requires seven completed siblings")
        completed_siblings += len(before_succeeded)
        expected_after_status = "succeeded" if decision == "approve" else "failed"
        if (
            any(after[index].get("status") != "succeeded" for index in range(7))
            or after[7].get("status") != expected_after_status
        ):
            raise RuntimeError("Workflow 2 child-pause resolution changed a sibling")
        if decision == "approve":
            if raw.get("terminal_output") != {"items": list(ITEMS)}:
                raise RuntimeError(
                    "approved Workflow 2 child-pause output is not exact and ordered"
                )
        elif raw.get("parent_failure_reason") != "parallel_execution_failed":
            raise RuntimeError("rejected Workflow 2 child-pause parent has the wrong failure")
        if (
            raw.get("signed_parent_chain") is not True
            or raw.get("signed_child_chain_count") != 8
            or raw.get("continuation_audit_count") != 1
            or raw.get("priced_call_count") != 0
        ):
            raise RuntimeError("Workflow 2 child-pause audit or zero-call proof failed")
        cost = raw.get("total_cost_usd")
        if not isinstance(cost, (int, float)) or isinstance(cost, bool) or float(cost) != 0.0:
            raise RuntimeError("Workflow 2 child-pause contains nonzero provider cost")
        total_cost += float(cost)
        parent_ids.append(str(parent_id))
    if len(set(parent_ids)) != 2:
        raise RuntimeError("Workflow 2 child-pause approve and reject reused a parent")
    return {
        "parent_run_ids": parent_ids,
        "completed_sibling_count": completed_siblings,
        "sibling_replay_count": 0,
        "priced_call_count": 0,
        "total_cost_usd": total_cost,
        "provider_economics_status": "blocked",
    }
