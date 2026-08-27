"""Provisioning-only public-API fixture for live provider batch evaluation."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from .provider_free_composed import Request, _object, _post, _publish_deploy_workflow

MODEL = "openai/gpt-4o-mini"


@dataclass(frozen=True, slots=True)
class ProviderBatchFixture:
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
    model: str = MODEL
    configured_items: int = 8
    configured_concurrency: int = 4
    provider_calls_per_child: int = 1
    provider_calls_performed: int = 0
    activated: bool = False
    invoked: bool = False
    sealed: bool = False


def provision_provider_batch_fixture(
    *, request: Request, fixture_id: str, items: Sequence[Mapping[str, object]]
) -> ProviderBatchFixture:
    """Publish child and parent v1 deployments without activating or executing either."""
    if re.fullmatch(r"[a-z0-9][a-z0-9-]{2,47}", fixture_id) is None:
        raise ValueError("fixture_id must be a short lowercase slug")
    normalized_items = tuple(dict(item) for item in items)
    if len(normalized_items) != 8 or any(
        set(item) != {"index", "query"}
        or isinstance(item.get("index"), bool)
        or item.get("index") != expected_index
        or not isinstance(item.get("query"), str)
        or not str(item["query"]).strip()
        for expected_index, item in enumerate(normalized_items)
    ):
        raise ValueError("items must be exactly eight ordered indexed queries")

    prefix = f"contract://live-provider-batch-{fixture_id}"
    item_contract = f"{prefix}.item"
    result_contract = f"{prefix}.result"
    batch_contract = f"{prefix}.batch"
    collected_contract = f"{prefix}.collected"
    item_schema: dict[str, object] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["index", "query"],
        "properties": {
            "index": {"type": "integer", "minimum": 0, "maximum": 7},
            "query": {"type": "string", "minLength": 1, "maxLength": 2000},
        },
    }
    result_schema: dict[str, object] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["index", "query", "finding", "confidence"],
        "properties": {
            "index": {"type": "integer", "minimum": 0, "maximum": 7},
            "query": {"type": "string", "minLength": 1, "maxLength": 2000},
            "finding": {"type": "string", "minLength": 1, "maxLength": 800},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        },
    }
    exact_eight_input = {
        "type": "array",
        "minItems": 8,
        "maxItems": 8,
        "items": item_schema,
    }
    exact_eight_results = {
        "type": "array",
        "minItems": 8,
        "maxItems": 8,
        "items": result_schema,
    }
    batch_schema: dict[str, object] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["items"],
        "properties": {"items": exact_eight_input},
    }
    collected_schema: dict[str, object] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "items": exact_eight_results,
            "results": exact_eight_results,
        },
        "oneOf": [{"required": ["items"]}, {"required": ["results"]}],
    }
    for name, schema in (
        (item_contract, item_schema),
        (result_contract, result_schema),
        (batch_contract, batch_schema),
        (collected_contract, collected_schema),
    ):
        contract = _post(
            request,
            "/api/studio/v1/contracts",
            {
                "name": name,
                "json_schema": schema,
                "metadata": {
                    "campaign_slice": "live-provider-batch-economics",
                    "fixture_id": fixture_id,
                    "provider_calls_performed": 0,
                },
            },
            expected=201,
            label=f"create contract {name}",
        )
        if contract.get("name") != name or contract.get("version") != 1:
            raise RuntimeError(f"contract {name} is not a new immutable v1 fixture")

    child_id = _create_workflow(
        request, name=f"Live provider batch child {fixture_id}"
    )
    child_deployment_ref = f"live-provider-batch-{fixture_id}-child"
    child_nodes = [
        _node(
            "item",
            "entrypoint",
            label="Indexed investigation",
            x=0,
            input_contract=item_contract,
            output_contract=item_contract,
        ),
        _node(
            "analyze",
            "agent",
            label="One-call concise analysis",
            x=320,
            input_contract=item_contract,
            output_contract=result_contract,
            config={
                "instruction": (
                    "Analyze this single operational query. Preserve index exactly and copy "
                    "query exactly. Return one concise finding of at most two sentences and "
                    "a confidence of low, medium, or high. Return only the structured result."
                ),
                "model_provider": MODEL,
                "model_params": {"temperature": 0, "max_tokens": 480},
                "max_tool_calls": 0,
                "tool_refs": [],
                "tool_bindings": [],
                "timeout_seconds": 60,
                "thread_participation": "none",
            },
        ),
    ]
    _save_workflow(
        request,
        workflow_id=child_id,
        nodes=child_nodes,
        edges=[
            {"id": "item-analyze", "source": "item", "target": "analyze", "kind": "data"}
        ],
        max_steps=4,
    )
    child_graph_ref, child_deployment_version = _publish_v1(
        request=request,
        workflow_id=child_id,
        deployment_ref=child_deployment_ref,
    )

    parent_id = _create_workflow(
        request, name=f"Live provider eight-item batch {fixture_id}"
    )
    parent_deployment_ref = f"live-provider-batch-{fixture_id}-parent"
    join_source = "\n".join(
        (
            "import json",
            "import sys",
            "payload = json.load(sys.stdin)",
            "results = sorted(payload['items'], key=lambda row: row['index'])",
            "if [row['index'] for row in results] != list(range(8)):",
            "    raise RuntimeError('batch result indexes are incomplete or duplicated')",
            "json.dump({'results': results}, sys.stdout, sort_keys=True)",
        )
    )
    parent_nodes = [
        _node(
            "batch",
            "entrypoint",
            label="Eight indexed queries",
            x=0,
            input_contract=batch_contract,
            output_contract=batch_contract,
            parallel_config={
                "split_path": "items",
                "merge_strategy": "collect",
                "fail_mode": "fail_fast",
                "max_branches": 8,
                "max_concurrency": 4,
                "batch_size": 8,
                "branch_timeout_seconds": 90,
            },
        ),
        _node(
            "analyze-child",
            "subgraph",
            label="Isolated one-call child",
            x=320,
            input_contract=item_contract,
            output_contract=result_contract,
            config={
                "graph_ref": child_deployment_ref,
                "version": child_deployment_version,
                "thread_participation": "isolated",
                "max_depth": 1,
            },
        ),
        _node(
            "ordered-join",
            "code",
            label="Deterministic ordered join",
            x=640,
            input_contract=collected_contract,
            output_contract=collected_contract,
            config={
                "execution_mode": "inline",
                "inline_source": join_source,
                "timeout_seconds": 10,
                "output_extraction_strategy": "json_stdout",
            },
            join_config={"merge_strategy": "collect", "merge_path": "items"},
        ),
    ]
    _save_workflow(
        request,
        workflow_id=parent_id,
        nodes=parent_nodes,
        edges=[
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
        ],
        max_steps=24,
    )
    parent_graph_ref, parent_deployment_version = _publish_v1(
        request=request,
        workflow_id=parent_id,
        deployment_ref=parent_deployment_ref,
    )
    return ProviderBatchFixture(
        schema_version=1,
        fixture_id=fixture_id,
        child_workflow_id=child_id,
        child_graph_version_ref=child_graph_ref,
        child_deployment_ref=child_deployment_ref,
        child_deployment_version=child_deployment_version,
        parent_workflow_id=parent_id,
        parent_graph_version_ref=parent_graph_ref,
        parent_deployment_ref=parent_deployment_ref,
        parent_deployment_version=parent_deployment_version,
    )


def write_unsealed_provider_batch_manifest(
    destination: Path, fixture: ProviderBatchFixture
) -> Path:
    """Write private, exclusive staging metadata without claiming acceptance."""
    destination = destination.expanduser().resolve(strict=False)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {**asdict(fixture), "evidence_status": "staging"}
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, sort_keys=True, indent=2)
        stream.write("\n")
    return destination


def _node(
    node_id: str,
    node_type: str,
    *,
    label: str,
    x: int,
    input_contract: str,
    output_contract: str,
    config: Mapping[str, object] | None = None,
    parallel_config: Mapping[str, object] | None = None,
    join_config: Mapping[str, object] | None = None,
) -> dict[str, object]:
    data: dict[str, object] = {
        "label": label,
        "config": dict(config or {}),
        "input_contract_ref": input_contract,
        "output_contract_ref": output_contract,
    }
    if parallel_config is not None:
        data["parallel_config"] = dict(parallel_config)
    if join_config is not None:
        data["join_config"] = dict(join_config)
    return {
        "id": node_id,
        "type": node_type,
        "position": {"x": x, "y": 0},
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


def _save_workflow(
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
                    "max_total_runtime_seconds": 180,
                    "max_visits_per_node": 8,
                    "default_timeout_seconds": 90,
                },
                "metadata": {
                    "evaluation_workflow": "live-provider-batch-v2",
                    "ordered_fan_in": True,
                    "preserve_input_index_order": True,
                },
            },
        ),
        expected=200,
        label=f"save {workflow_id}",
    )


def _publish_v1(
    *, request: Request, workflow_id: str, deployment_ref: str
) -> tuple[str, int]:
    graph_ref, deployment_version = _publish_deploy_workflow(
        request=request,
        workflow_id=workflow_id,
        deployment_ref=deployment_ref,
    )
    if graph_ref != f"{workflow_id}@1" or deployment_version != 1:
        raise RuntimeError(f"deployment {deployment_ref} is not a new immutable v1 fixture")
    return graph_ref, deployment_version
