"""Provider-free persistent fixture and validators for child-owned approvals.

The fixture is authored only through public Studio/deployment APIs.  It creates
one structured-token parent with two child branches: a deterministic sibling
that can be durably delivered to the join, and a child-owned approval gate.
The module never resolves a provider credential and never claims provider
economics; callers must keep that verdict explicitly blocked.
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .provider_free_composed import (
    Request,
    _object,
    _post,
    _publish_deploy_workflow,
    _require_immutable_snapshot,
)


@dataclass(frozen=True, slots=True)
class ProviderFreeChildApprovalFixture:
    schema_version: int
    fixture_id: str
    durable_workflow_id: str
    durable_deployment_ref: str
    approval_workflow_id: str
    approval_deployment_ref: str
    collector_workflow_id: str
    collector_deployment_ref: str
    parent_workflow_id: str
    parent_graph_version_ref: str
    parent_deployment_ref: str
    parent_deployment_version: int
    payload: dict[str, str]
    provider_calls_performed: int = 0
    provider_economics_status: str = "blocked"


@dataclass(frozen=True, slots=True)
class StagedChildApproval:
    """Sanitized identities captured while the parent is durably paused."""

    parent_run_id: str
    approval_id: str
    approval_child_run_id: str
    durable_child_run_id: str
    container_started_at: str


def _node(
    node_id: str,
    node_type: str,
    *,
    label: str,
    x: int,
    y: int = 0,
    config: Mapping[str, object] | None = None,
    contract_ref: str,
    join_config: Mapping[str, object] | None = None,
) -> dict[str, object]:
    data: dict[str, object] = {
        "label": label,
        "config": dict(config or {}),
        "input_contract_ref": contract_ref,
        "output_contract_ref": contract_ref,
    }
    if join_config is not None:
        data["join_config"] = dict(join_config)
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
                    "max_total_runtime_seconds": 60,
                    "max_visits_per_node": 2,
                    "default_timeout_seconds": 30,
                },
            },
        ),
        expected=200,
        label=f"save {workflow_id}",
    )


def provision_child_approval_fixture(
    *, request: Request, fixture_id: str
) -> ProviderFreeChildApprovalFixture:
    """Publish an exact two-branch child-approval fixture through public APIs."""
    if not fixture_id or len(fixture_id) > 48 or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in fixture_id
    ):
        raise ValueError("fixture_id must be a short lowercase slug")
    contract_ref = f"contract://provider-free-child-approval-{fixture_id}"
    contract = _post(
        request,
        "/api/studio/v1/contracts",
        {
            "name": contract_ref,
            "json_schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "request": {"type": "string", "const": "d012-provider-free"},
                    "branch": {"type": "string", "enum": ["durable", "approval"]},
                    "branches": {
                        "type": "array",
                        "items": {"type": "object"},
                    },
                },
            },
            "metadata": {
                "campaign_slice": "d012-child-approval-live",
                "provider_calls_performed": 0,
            },
        },
        expected=201,
        label="create D-012 contract",
    )
    if contract.get("name") != contract_ref or contract.get("version") != 1:
        raise RuntimeError("D-012 contract is not a new immutable v1 fixture")

    durable_deployment = f"provider-free-child-approval-{fixture_id}-durable"
    durable_id = _create_workflow(request, name=f"D-012 durable child {fixture_id}")
    _save(
        request,
        workflow_id=durable_id,
        nodes=[
            _node(
                "durable",
                "entrypoint",
                label="Durably delivered sibling",
                x=0,
                contract_ref=contract_ref,
            )
        ],
        edges=[],
        max_steps=4,
    )
    _, durable_version = _publish_deploy_workflow(
        request=request,
        workflow_id=durable_id,
        deployment_ref=durable_deployment,
    )

    approval_deployment = f"provider-free-child-approval-{fixture_id}-approval"
    approval_id = _create_workflow(request, name=f"D-012 approval child {fixture_id}")
    _save(
        request,
        workflow_id=approval_id,
        nodes=[
            _node(
                "entry",
                "entrypoint",
                label="Approval branch input",
                x=0,
                contract_ref=contract_ref,
            ),
            _node(
                "approve",
                "human_approval",
                label="Review child branch",
                x=280,
                config={
                    "approval_payload_schema_ref": contract_ref,
                    "resolution_schema_ref": contract_ref,
                    "approval_policy_config": {"allow_edits": False},
                },
                contract_ref=contract_ref,
            )
        ],
        edges=[
            {
                "id": "entry-approve",
                "source": "entry",
                "target": "approve",
                "kind": "data",
            }
        ],
        max_steps=4,
    )
    _, approval_version = _publish_deploy_workflow(
        request=request,
        workflow_id=approval_id,
        deployment_ref=approval_deployment,
    )

    collector_deployment = f"provider-free-child-approval-{fixture_id}-collector"
    collector_id = _create_workflow(request, name=f"D-012 collector child {fixture_id}")
    _save(
        request,
        workflow_id=collector_id,
        nodes=[
            _node(
                "collect",
                "entrypoint",
                label="Collect exact branches",
                x=0,
                contract_ref=contract_ref,
            )
        ],
        edges=[],
        max_steps=4,
    )
    _, collector_version = _publish_deploy_workflow(
        request=request,
        workflow_id=collector_id,
        deployment_ref=collector_deployment,
    )

    parent_deployment = f"provider-free-child-approval-{fixture_id}-parent"
    parent_id = _create_workflow(request, name=f"D-012 structured child approval {fixture_id}")
    parent_nodes = [
        _node("entry", "entrypoint", label="Provider-free request", x=0, contract_ref=contract_ref),
        _node(
            "durable-child",
            "subgraph",
            label="Durable sibling",
            x=320,
            y=-120,
            config={
                "graph_ref": durable_deployment,
                "version": durable_version,
                "thread_participation": "isolated",
                "max_depth": 1,
            },
            contract_ref=contract_ref,
        ),
        _node(
            "approval-child",
            "subgraph",
            label="Approval child",
            x=320,
            y=120,
            config={
                "graph_ref": approval_deployment,
                "version": approval_version,
                "thread_participation": "isolated",
                "max_depth": 1,
            },
            contract_ref=contract_ref,
        ),
        _node(
            "collector",
            "subgraph",
            label="Structured collector",
            x=640,
            config={
                "graph_ref": collector_deployment,
                "version": collector_version,
                "thread_participation": "isolated",
                "max_depth": 1,
            },
            join_config={"merge_strategy": "collect", "merge_path": "branches"},
            contract_ref=contract_ref,
        ),
    ]
    parent_edges = [
        {
            "id": "entry-durable",
            "source": "entry",
            "target": "durable-child",
            "kind": "data",
            "mapping": {
                "operations": [
                    {"operation": "constant", "target_path": "branch", "value": "durable"}
                ]
            },
        },
        {
            "id": "entry-approval",
            "source": "entry",
            "target": "approval-child",
            "kind": "data",
            "mapping": {
                "operations": [
                    {"operation": "constant", "target_path": "branch", "value": "approval"}
                ]
            },
        },
        {
            "id": "durable-collector",
            "source": "durable-child",
            "target": "collector",
            "kind": "data",
        },
        {
            "id": "approval-collector",
            "source": "approval-child",
            "target": "collector",
            "kind": "data",
        },
    ]
    _save(
        request,
        workflow_id=parent_id,
        nodes=parent_nodes,
        edges=parent_edges,
        max_steps=20,
    )
    parent_graph_ref, parent_version = _publish_deploy_workflow(
        request=request,
        workflow_id=parent_id,
        deployment_ref=parent_deployment,
    )
    return ProviderFreeChildApprovalFixture(
        schema_version=1,
        fixture_id=fixture_id,
        durable_workflow_id=durable_id,
        durable_deployment_ref=durable_deployment,
        approval_workflow_id=approval_id,
        approval_deployment_ref=approval_deployment,
        collector_workflow_id=collector_id,
        collector_deployment_ref=collector_deployment,
        parent_workflow_id=parent_id,
        parent_graph_version_ref=parent_graph_ref,
        parent_deployment_ref=parent_deployment,
        parent_deployment_version=parent_version,
        payload={"request": "d012-provider-free"},
    )


def recover_child_approval_fixture(
    *, request: Request, fixture_id: str
) -> ProviderFreeChildApprovalFixture:
    """Recover one complete public-API fixture without creating another version."""
    if not fixture_id or len(fixture_id) > 48 or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in fixture_id
    ):
        raise ValueError("fixture_id must be a short lowercase slug")

    def list_response(path: str, *, label: str) -> list[object]:
        response = request("GET", path, None)
        if response.status_code != 200:
            raise RuntimeError(f"D-012 {label} lookup failed")
        value = response.json()
        if not isinstance(value, list):
            raise RuntimeError(f"D-012 {label} lookup is malformed")
        return value

    workflows = list_response("/api/studio/v1/workflows", label="workflow")
    expected_names = {
        "durable": f"D-012 durable child {fixture_id}",
        "approval": f"D-012 approval child {fixture_id}",
        "collector": f"D-012 collector child {fixture_id}",
        "parent": f"D-012 structured child approval {fixture_id}",
    }
    workflow_ids: dict[str, str] = {}
    for kind, name in expected_names.items():
        matches = [
            item
            for item in workflows
            if isinstance(item, Mapping) and item.get("name") == name
        ]
        if len(matches) != 1 or not isinstance(matches[0].get("id"), str):
            raise RuntimeError(f"D-012 existing {kind} workflow is missing or ambiguous")
        workflow_ids[kind] = str(matches[0]["id"])

    deployments = list_response("/v1/deployments", label="deployment")
    deployment_rows: dict[str, Mapping[str, object]] = {}
    for kind in expected_names:
        deployment_ref = f"provider-free-child-approval-{fixture_id}-{kind}"
        matches = [
            item
            for item in deployments
            if isinstance(item, Mapping) and item.get("deployment_ref") == deployment_ref
        ]
        if len(matches) != 1:
            raise RuntimeError(f"D-012 existing {kind} deployment is missing or ambiguous")
        row = matches[0]
        version = row.get("version")
        graph_ref = row.get("graph_version_ref")
        if version != 1 or graph_ref != f"{workflow_ids[kind]}@1":
            raise RuntimeError(f"D-012 existing {kind} deployment identity drifted")
        deployment_rows[kind] = row

    return ProviderFreeChildApprovalFixture(
        schema_version=1,
        fixture_id=fixture_id,
        durable_workflow_id=workflow_ids["durable"],
        durable_deployment_ref=str(deployment_rows["durable"]["deployment_ref"]),
        approval_workflow_id=workflow_ids["approval"],
        approval_deployment_ref=str(deployment_rows["approval"]["deployment_ref"]),
        collector_workflow_id=workflow_ids["collector"],
        collector_deployment_ref=str(deployment_rows["collector"]["deployment_ref"]),
        parent_workflow_id=workflow_ids["parent"],
        parent_graph_version_ref=str(deployment_rows["parent"]["graph_version_ref"]),
        parent_deployment_ref=str(deployment_rows["parent"]["deployment_ref"]),
        parent_deployment_version=1,
        payload={"request": "d012-provider-free"},
    )


def validate_child_approval_summary(summary: Mapping[str, object]) -> dict[str, object]:
    """Fail closed on the approve/reject UI and durable-linkage observations."""
    if (
        summary.get("schema_version") != 1
        or summary.get("provider_calls_performed") != 0
        or summary.get("provider_economics_status") != "blocked"
        or summary.get("restart_count") != 1
    ):
        raise RuntimeError("D-012 summary identity or provider-free boundary is invalid")
    raw_approvals = summary.get("approvals")
    if not isinstance(raw_approvals, list) or len(raw_approvals) != 2:
        raise RuntimeError("D-012 summary requires one approve and one reject checkpoint")
    expected_status = {"approve": "succeeded", "reject": "failed"}
    seen: set[str] = set()
    parent_ids: list[str] = []
    aggregate_cost = 0.0
    for raw in raw_approvals:
        if not isinstance(raw, Mapping):
            raise RuntimeError("D-012 approval checkpoint must be an object")
        decision = raw.get("decision")
        if decision not in expected_status or decision in seen:
            raise RuntimeError("D-012 decisions must be exactly approve and reject")
        seen.add(str(decision))
        reason = raw.get("reason")
        approval_id = raw.get("approval_id")
        child_id = raw.get("child_run_id")
        parent_id = raw.get("parent_run_id")
        identities = (reason, approval_id, child_id, parent_id)
        if not all(isinstance(value, str) and value for value in identities):
            raise RuntimeError("D-012 checkpoint lacks a durable identity or reviewer reason")
        if raw.get("parent_status") != expected_status[str(decision)]:
            raise RuntimeError("D-012 parent terminal status does not match the decision")
        if (
            raw.get("durable_sibling_delivery_count_before") != 1
            or raw.get("durable_sibling_delivery_count_after") != 1
            or raw.get("continuation_audit_count") != 1
            or raw.get("signed_audit") is not True
            or raw.get("priced_call_count") != 0
            or raw.get("restored_after_refresh") is not True
            or raw.get("restored_after_restart") is not True
        ):
            raise RuntimeError("D-012 linkage, restart, exactly-once, or zero-call gate failed")
        cost = raw.get("total_cost_usd")
        if not isinstance(cost, (int, float)) or isinstance(cost, bool) or float(cost) != 0.0:
            raise RuntimeError("D-012 provider-free checkpoint contains nonzero cost")
        aggregate_cost += float(cost)
        parent_ids.append(str(parent_id))
    if len(set(parent_ids)) != 2:
        raise RuntimeError("D-012 approve and reject checkpoints reused a parent run")
    return {
        "parent_run_ids": parent_ids,
        "provider_calls_performed": 0,
        "aggregate_cost_usd": aggregate_cost,
        "provider_economics_status": "blocked",
    }


def validate_child_approval_snapshots(
    before: Path,
    after: Path,
    *,
    tenant_id: str,
    fixture: ProviderFreeChildApprovalFixture,
    staged: StagedChildApproval,
    summary: Mapping[str, object],
) -> dict[str, object]:
    """Reconcile closed pre/post-restart DB images with the browser identities."""
    validated = validate_child_approval_summary(summary)
    for database in (before, after):
        _require_immutable_snapshot(database.expanduser().resolve(strict=True))

    def connect(path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"file:{path.expanduser().resolve(strict=True)}?mode=ro&immutable=1",
            uri=True,
        )
        connection.row_factory = sqlite3.Row
        return connection

    with connect(before) as connection:
        parent = connection.execute(
            "SELECT status FROM runs WHERE tenant_id = ? AND run_id = ?",
            (tenant_id, staged.parent_run_id),
        ).fetchone()
        if parent is None or str(parent["status"]).upper() != "WAITING_APPROVAL":
            raise RuntimeError("D-012 pre-restart parent is not durably paused")
        durable_before = connection.execute(
            """
            SELECT execution_history FROM runs
            WHERE tenant_id = ? AND run_id = ? AND parent_run_id = ?
              AND deployment_ref = ? AND status = 'COMPLETED'
            """,
            (
                tenant_id,
                staged.durable_child_run_id,
                staged.parent_run_id,
                fixture.durable_deployment_ref,
            ),
        ).fetchone()
        if durable_before is None:
            raise RuntimeError("D-012 durable sibling is absent before restart")
        snapshot_row = connection.execute(
            "SELECT snapshot_json FROM token_engine_snapshots WHERE tenant_id = ? AND run_id = ?",
            (tenant_id, staged.parent_run_id),
        ).fetchone()
        if snapshot_row is None:
            raise RuntimeError("D-012 pre-restart token snapshot is missing")
        snapshot = json.loads(snapshot_row["snapshot_json"])
        joins = snapshot.get("joins") if isinstance(snapshot, dict) else None
        collector = [
            join
            for join in joins or []
            if isinstance(join, dict) and join.get("target_node_id") == "collector"
        ]
        if len(collector) != 1:
            raise RuntimeError("D-012 pre-restart collector checkpoint is missing")
        obligations = collector[0].get("obligations")
        if not isinstance(obligations, list):
            raise RuntimeError("D-012 collector obligations are malformed")
        delivered = [
            item
            for item in obligations
            if isinstance(item, dict) and item.get("delivery") is not None
        ]
        if len(delivered) != 1 or delivered[0]["delivery"].get("payload") != {
            "branch": "durable"
        }:
            raise RuntimeError("D-012 durable sibling was not singly delivered before restart")
        durable_history_before = durable_before["execution_history"]

    checkpoints = summary.get("approvals")
    assert isinstance(checkpoints, list)  # proved by validate_child_approval_summary
    signed_count = 0
    priced_count = 0
    total_cost = 0.0
    with connect(after) as connection:
        for checkpoint in checkpoints:
            assert isinstance(checkpoint, Mapping)
            parent_id = str(checkpoint["parent_run_id"])
            child_id = str(checkpoint["child_run_id"])
            approval_id = str(checkpoint["approval_id"])
            decision = str(checkpoint["decision"])
            expected_parent = "COMPLETED" if decision == "approve" else "FAILED"
            parent = connection.execute(
                "SELECT status FROM runs WHERE tenant_id = ? AND run_id = ? AND deployment_ref = ?",
                (tenant_id, parent_id, fixture.parent_deployment_ref),
            ).fetchone()
            if parent is None or str(parent["status"]).upper() != expected_parent:
                raise RuntimeError("D-012 post-restart parent verdict drifted")
            durable_rows = connection.execute(
                """
                SELECT run_id, execution_history FROM runs
                WHERE tenant_id = ? AND parent_run_id = ? AND deployment_ref = ?
                """,
                (tenant_id, parent_id, fixture.durable_deployment_ref),
            ).fetchall()
            if len(durable_rows) != 1:
                raise RuntimeError("D-012 durable sibling was replayed or lost")
            if parent_id == staged.parent_run_id and (
                durable_rows[0]["run_id"] != staged.durable_child_run_id
                or durable_rows[0]["execution_history"] != durable_history_before
            ):
                raise RuntimeError("D-012 staged durable sibling changed across restart")
            child = connection.execute(
                """
                SELECT run_id FROM runs
                WHERE tenant_id = ? AND run_id = ? AND parent_run_id = ? AND deployment_ref = ?
                """,
                (tenant_id, child_id, parent_id, fixture.approval_deployment_ref),
            ).fetchone()
            if child is None:
                raise RuntimeError("D-012 approval child lineage changed after restart")
            approval_row = connection.execute(
                "SELECT record_json FROM approvals "
                "WHERE tenant_id = ? AND approval_id = ? AND run_id = ?",
                (tenant_id, approval_id, child_id),
            ).fetchone()
            if approval_row is None:
                raise RuntimeError("D-012 resolved child approval is missing")
            approval = json.loads(approval_row["record_json"])
            resolution = approval.get("resolution") if isinstance(approval, dict) else None
            if not isinstance(resolution, dict) or (
                resolution.get("decision") != decision
                or resolution.get("reason") != checkpoint["reason"]
            ):
                raise RuntimeError("D-012 durable decision reason or verdict drifted")
            audit_rows = connection.execute(
                "SELECT cost_usd, cost_event_id, record_json FROM node_audits "
                "WHERE tenant_id = ? AND run_id = ?",
                (tenant_id, parent_id),
            ).fetchall()
            continuations = []
            for row in audit_rows:
                record = json.loads(row["record_json"])
                cost = float(row["cost_usd"] or 0.0)
                total_cost += cost
                is_priced = (
                    row["cost_event_id"] is not None
                    or record.get("token_usage") is not None
                    or cost != 0.0
                    or float(record.get("estimated_cost_usd") or 0.0) != 0.0
                )
                priced_count += int(is_priced)
                if record.get("status") == "child_approval_continuation_scheduled":
                    continuations.append(record)
            if len(continuations) != 1:
                raise RuntimeError("D-012 continuation audit is missing or duplicated")
            continuation = continuations[0]
            metadata = continuation.get("execution_metadata")
            if (
                not continuation.get("record_signature")
                or continuation.get("audit_id")
                != f"{parent_id}:child-approval-continuation:{approval_id}"
                or not isinstance(metadata, dict)
                or metadata.get("child_run_id") != child_id
                or metadata.get("continuation_parent_run_id") != parent_id
            ):
                raise RuntimeError("D-012 signed parent/child audit linkage is incomplete")
            signed_count += 1
    if priced_count != 0 or total_cost != 0.0 or validated["aggregate_cost_usd"] != 0.0:
        raise RuntimeError("D-012 provider-free snapshots contain priced or nonzero activity")
    return {
        "partial_delivery_count_before_restart": 1,
        "durable_sibling_replay_count": 0,
        "signed_continuation_count": signed_count,
        "priced_call_count": priced_count,
        "total_cost_usd": total_cost,
        "provider_economics_status": "blocked",
    }


def stage_pending_child_approval(
    *,
    request: Request,
    fixture: ProviderFreeChildApprovalFixture,
    container_started_at: str,
    timeout_seconds: float = 30.0,
) -> StagedChildApproval:
    """Submit one parent and capture its exact child-owned approval before restart."""
    if not container_started_at:
        raise ValueError("container start identity is required")
    if not 0 < timeout_seconds <= 60:
        raise ValueError("D-012 staging timeout must be positive and bounded")
    created = _post(
        request,
        "/v1/runs",
        {
            "input_payload": fixture.payload,
            "campaign_id": "evaluation-studio-v1",
            "campaign_strict": True,
        },
        expected=202,
        label="submit D-012 parent",
    )
    parent_id = created.get("run_id")
    if not isinstance(parent_id, str) or not parent_id:
        raise RuntimeError("D-012 run submission lacks a parent identity")
    deadline = time.monotonic() + timeout_seconds
    parent: dict[str, object] | None = None
    while time.monotonic() < deadline:
        parent = _object(
            request("GET", f"/v1/runs/{parent_id}", None),
            expected=200,
            label="read D-012 parent",
        )
        if parent.get("status") == "paused_for_approval":
            break
        if parent.get("status") in {"succeeded", "failed", "terminated_by_policy"}:
            raise RuntimeError("D-012 parent terminalized before its child approval")
        time.sleep(0.05)
    else:
        raise RuntimeError("D-012 parent did not pause within the bounded staging window")
    if (
        parent.get("deployment_ref") != fixture.parent_deployment_ref
        or parent.get("graph_version_ref") != fixture.parent_graph_version_ref
    ):
        raise RuntimeError("D-012 parent serving identity drifted during staging")

    approvals_response = request(
        "GET",
        (
            f"/v1/deployments/{fixture.parent_deployment_ref}/approvals"
            f"?run_id={parent_id}"
        ),
        None,
    )
    if approvals_response.status_code != 200:
        raise RuntimeError("D-012 parent-scoped approval lookup failed")
    approvals = approvals_response.json()
    if not isinstance(approvals, list) or len(approvals) != 1:
        raise RuntimeError("D-012 parent must expose exactly one child approval")
    approval = approvals[0]
    if not isinstance(approval, Mapping):
        raise RuntimeError("D-012 child approval response is malformed")
    approval_id = approval.get("approval_id")
    approval_child_id = approval.get("run_id")
    if (
        not isinstance(approval_id, str)
        or not isinstance(approval_child_id, str)
        or approval.get("deployment_ref") != fixture.approval_deployment_ref
        or approval.get("status") != "pending"
    ):
        raise RuntimeError("D-012 child approval identity or provenance drifted")

    children_response = request("GET", f"/v1/runs/{parent_id}/children", None)
    if children_response.status_code != 200:
        raise RuntimeError("D-012 child lineage lookup failed")
    children = children_response.json()
    if not isinstance(children, list):
        raise RuntimeError("D-012 child lineage response is malformed")
    durable = [
        child
        for child in children
        if isinstance(child, Mapping)
        and child.get("deployment_ref") == fixture.durable_deployment_ref
    ]
    if len(durable) != 1 or durable[0].get("status") != "succeeded":
        raise RuntimeError("D-012 durable sibling was not delivered before the approval pause")
    durable_id = durable[0].get("run_id")
    if not isinstance(durable_id, str) or not durable_id:
        raise RuntimeError("D-012 durable sibling identity is missing")
    return StagedChildApproval(
        parent_run_id=parent_id,
        approval_id=approval_id,
        approval_child_run_id=approval_child_id,
        durable_child_run_id=durable_id,
        container_started_at=container_started_at,
    )


class BoundedChildApprovalUiRunner:
    """Run only the fixed D-012 spec after an externally coordinated restart."""

    _TITLE = "resolves exact child approve and reject after one coordinated restart"

    def __init__(
        self,
        *,
        frontend_root: Path,
        environment: Mapping[str, str],
        timeout_seconds: int = 180,
    ) -> None:
        self.frontend_root = frontend_root.resolve(strict=True)
        self.spec = self.frontend_root / "e2e/child-approval-live.spec.ts"
        if self.frontend_root.name != "frontend" or not self.spec.is_file():
            raise ValueError("D-012 UI runner requires the repository frontend root")
        if not 30 <= timeout_seconds <= 300:
            raise ValueError("D-012 UI timeout must be bounded")
        self.environment = dict(environment)
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _summary(report: object) -> dict[str, object]:
        matches: list[dict[str, object]] = []

        def visit(value: object) -> None:
            if isinstance(value, dict):
                if value.get("name") == "child-approval-live-summary" and isinstance(
                    value.get("body"), str
                ):
                    try:
                        decoded = json.loads(base64.b64decode(value["body"], validate=True))
                    except (ValueError, json.JSONDecodeError) as exc:
                        raise RuntimeError("D-012 Playwright attachment is malformed") from exc
                    if isinstance(decoded, dict):
                        matches.append(decoded)
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(report)
        if len(matches) != 1:
            raise RuntimeError("D-012 Playwright report requires one summary attachment")
        return matches[0]

    @classmethod
    def _persist_outputs(
        cls,
        *,
        report: object,
        browser_root: Path,
        frontend_root: Path,
        raw_summary: Mapping[str, object],
        validated_summary: Mapping[str, object],
    ) -> Path:
        """Materialize the fixed JSON report and its sanitized attachments."""
        browser_root = browser_root.expanduser().resolve(strict=False)
        browser_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        artifacts: list[dict[str, str]] = []

        def safe_name(value: str) -> str:
            normalized = "".join(
                character.lower() if character.isalnum() else "-" for character in value
            ).strip("-")
            return normalized[:80] or "attachment"

        def write(relative: Path, payload: bytes) -> None:
            destination = browser_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            artifacts.append(
                {"source": relative.as_posix(), "destination": relative.as_posix()}
            )

        write(
            Path("playwright-report/results.json"),
            (json.dumps(report, indent=2, sort_keys=True) + "\n").encode(),
        )
        write(
            Path("console/raw-playwright-summary.json"),
            (json.dumps(raw_summary, indent=2, sort_keys=True) + "\n").encode(),
        )
        write(
            Path("console/validated-playwright-summary.json"),
            (json.dumps(validated_summary, indent=2, sort_keys=True) + "\n").encode(),
        )
        attachment_index = 0

        def visit(value: object) -> None:
            nonlocal attachment_index
            if isinstance(value, dict):
                name = value.get("name")
                content_type = value.get("contentType")
                if isinstance(name, str) and isinstance(content_type, str):
                    payload: bytes | None = None
                    body = value.get("body")
                    source_path = value.get("path")
                    if isinstance(body, str):
                        try:
                            payload = base64.b64decode(body, validate=True)
                        except ValueError as exc:
                            raise RuntimeError(
                                "D-012 Playwright attachment body is malformed"
                            ) from exc
                    elif isinstance(source_path, str):
                        source = Path(source_path)
                        if not source.is_absolute():
                            source = frontend_root / source
                        source = source.resolve(strict=True)
                        if source.is_symlink() or not source.is_file():
                            raise RuntimeError("D-012 Playwright attachment path is unsafe")
                        payload = source.read_bytes()
                    if payload is not None and name != "child-approval-live-summary":
                        attachment_index += 1
                        if content_type == "image/png":
                            category, suffix = "screenshots", ".png"
                        elif content_type == "video/webm":
                            category, suffix = "videos", ".webm"
                        elif "network" in name:
                            category, suffix = "network", ".json"
                        else:
                            category, suffix = "console", ".json"
                        write(
                            Path(category)
                            / f"{attachment_index:02d}-{safe_name(name)}{suffix}",
                            payload,
                        )
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(report)
        index = {
            "schema_version": 1,
            "completed": True,
            "fixed_title": cls._TITLE,
            "artifacts": artifacts,
        }
        destination = browser_root / "results.json"
        with destination.open("x", encoding="utf-8") as handle:
            json.dump(index, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        return destination

    def run(
        self,
        fixture: ProviderFreeChildApprovalFixture,
        *,
        staged: StagedChildApproval,
        container_started_at_after: str,
    ) -> dict[str, object]:
        """Validate the external restart identity, then run the fixed browser proof."""
        if (
            not container_started_at_after
            or container_started_at_after == staged.container_started_at
        ):
            raise RuntimeError("D-012 requires one observed backend restart before UI resolve")
        argv = (
            "npm",
            "exec",
            "--",
            "playwright",
            "test",
            "e2e/child-approval-live.spec.ts",
            "--project=desktop-1440",
            "--grep",
            self._TITLE,
            "--reporter=json",
        )
        allowed = {
            "PATH",
            "HOME",
            "TMPDIR",
            "ZEROTH_EVALUATION_API_BASE",
            "ZEROTH_EVALUATION_API_KEY",
            "ZEROTH_EVALUATION_BASE_URL",
            "ZEROTH_EVALUATION_BROWSER_ROOT",
            "ZEROTH_EVALUATION_TENANT",
        }
        child_env = {
            name: value
            for name, value in {**os.environ, **self.environment}.items()
            if name in allowed
        }
        child_env.update(
            {
                "PLAYWRIGHT_NO_SERVER": "1",
                "ZEROTH_EVALUATION_LIVE": "1",
                "ZEROTH_EVALUATION_D012_PARENT_DEPLOYMENT_REF": (
                    fixture.parent_deployment_ref
                ),
                "ZEROTH_EVALUATION_D012_PARENT_GRAPH_VERSION": (
                    fixture.parent_graph_version_ref
                ),
                "ZEROTH_EVALUATION_D012_APPROVAL_DEPLOYMENT_REF": (
                    fixture.approval_deployment_ref
                ),
                "ZEROTH_EVALUATION_D012_DURABLE_DEPLOYMENT_REF": (
                    fixture.durable_deployment_ref
                ),
                "ZEROTH_EVALUATION_D012_PRE_RESTART_PARENT_RUN_ID": staged.parent_run_id,
                "ZEROTH_EVALUATION_D012_PRE_RESTART_APPROVAL_ID": staged.approval_id,
                "ZEROTH_EVALUATION_D012_PRE_RESTART_CHILD_RUN_ID": (
                    staged.approval_child_run_id
                ),
            }
        )
        completed = subprocess.run(
            argv,
            cwd=self.frontend_root,
            env=child_env,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError("bounded D-012 Playwright run failed")
        try:
            report = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("D-012 Playwright JSON report is malformed") from exc
        statuses: list[str] = []

        def collect(value: object, *, matched: bool = False) -> None:
            if isinstance(value, dict):
                matched = matched or value.get("title") == self._TITLE
                if matched and isinstance(value.get("results"), list):
                    statuses.extend(
                        str(result.get("status"))
                        for result in value["results"]
                        if isinstance(result, dict)
                    )
                for child in value.values():
                    collect(child, matched=matched)
            elif isinstance(value, list):
                for child in value:
                    collect(child, matched=matched)

        collect(report)
        if statuses != ["passed"]:
            raise RuntimeError("D-012 Playwright report lacks one passing fixed test")
        raw_summary = self._summary(report)
        validated_summary = validate_child_approval_summary(raw_summary)
        browser_root_value = child_env.get("ZEROTH_EVALUATION_BROWSER_ROOT")
        if not browser_root_value:
            raise RuntimeError("D-012 UI runner requires an external browser evidence root")
        self._persist_outputs(
            report=report,
            browser_root=Path(browser_root_value),
            frontend_root=self.frontend_root,
            raw_summary=raw_summary,
            validated_summary=validated_summary,
        )
        return {
            **validated_summary,
            "raw_summary": raw_summary,
            "validated_summary": validated_summary,
        }
