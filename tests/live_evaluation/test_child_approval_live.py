from __future__ import annotations

import base64
import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from zeroth.governance.approvals.models import ApprovalDecision, ApprovalResolution
from zeroth.governance.identity import ActorIdentity, AuthMethod
from zeroth.service.api.approval_api import ApprovalResolutionRequest

from release.live_evaluation.child_approval_live import (
    BoundedChildApprovalUiRunner,
    ProviderFreeChildApprovalFixture,
    StagedChildApproval,
    provision_child_approval_fixture,
    stage_pending_child_approval,
    validate_child_approval_snapshots,
    validate_child_approval_summary,
)


class _Response:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self) -> object:
        return self._payload


def test_resolution_reason_is_an_additive_durable_api_field() -> None:
    request = ApprovalResolutionRequest(
        decision=ApprovalDecision.APPROVE,
        reason="Verified exact child and durable sibling",
    )
    resolution = ApprovalResolution(
        decision=request.decision,
        actor=ActorIdentity(subject="reviewer", auth_method=AuthMethod.API_KEY),
        reason=request.reason,
    )

    assert resolution.reason == "Verified exact child and durable sibling"


def test_provider_free_child_approval_fixture_publishes_exact_structured_branches() -> None:
    calls: list[tuple[str, str, dict[str, Any] | None]] = []
    workflow_ids = iter(("durable-graph", "approval-graph", "collector-graph", "parent-graph"))

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

    fixture = provision_child_approval_fixture(request=request, fixture_id="d012-live")

    assert isinstance(fixture, ProviderFreeChildApprovalFixture)
    assert fixture.provider_calls_performed == 0
    assert fixture.provider_economics_status == "blocked"
    saves = [payload for method, path, payload in calls if method == "PUT"]
    assert len(saves) == 4
    assert [node["type"] for node in saves[1]["nodes"]] == ["entrypoint", "human_approval"]
    assert saves[1]["edges"] == [
        {
            "id": "entry-approve",
            "source": "entry",
            "target": "approve",
            "kind": "data",
        }
    ]
    parent = saves[3]
    assert [node["id"] for node in parent["nodes"]] == [
        "entry",
        "durable-child",
        "approval-child",
        "collector",
    ]
    assert parent["nodes"][3]["data"]["join_config"] == {
        "merge_strategy": "collect",
        "merge_path": "branches",
    }
    mappings = {
        edge["id"]: edge.get("mapping")
        for edge in parent["edges"]
        if edge["id"].startswith("entry-")
    }
    assert mappings == {
        "entry-durable": {
            "operations": [{"operation": "constant", "target_path": "branch", "value": "durable"}]
        },
        "entry-approval": {
            "operations": [{"operation": "constant", "target_path": "branch", "value": "approval"}]
        },
    }


def test_child_approval_summary_requires_ui_reasons_signed_linkage_and_zero_cost() -> None:
    summary = {
        "schema_version": 1,
        "provider_economics_status": "blocked",
        "provider_calls_performed": 0,
        "restart_count": 1,
        "approvals": [
            {
                "decision": "approve",
                "reason": "Verified durable sibling before approval",
                "approval_id": "approval-a",
                "child_run_id": "child-a",
                "parent_run_id": "parent-a",
                "parent_status": "succeeded",
                "durable_sibling_delivery_count_before": 1,
                "durable_sibling_delivery_count_after": 1,
                "continuation_audit_count": 1,
                "signed_audit": True,
                "priced_call_count": 0,
                "total_cost_usd": 0,
                "restored_after_refresh": True,
                "restored_after_restart": True,
            },
            {
                "decision": "reject",
                "reason": "Rejected controlled provider-free branch",
                "approval_id": "approval-b",
                "child_run_id": "child-b",
                "parent_run_id": "parent-b",
                "parent_status": "failed",
                "durable_sibling_delivery_count_before": 1,
                "durable_sibling_delivery_count_after": 1,
                "continuation_audit_count": 1,
                "signed_audit": True,
                "priced_call_count": 0,
                "total_cost_usd": 0,
                "restored_after_refresh": True,
                "restored_after_restart": True,
            },
        ],
    }

    result = validate_child_approval_summary(summary)

    assert result["parent_run_ids"] == ["parent-a", "parent-b"]
    assert result["provider_calls_performed"] == 0
    assert result["aggregate_cost_usd"] == 0


def test_stage_pending_child_approval_captures_exact_parent_child_identity() -> None:
    fixture = ProviderFreeChildApprovalFixture(
        schema_version=1,
        fixture_id="d012-live",
        durable_workflow_id="durable-graph",
        durable_deployment_ref="d012-durable",
        approval_workflow_id="approval-graph",
        approval_deployment_ref="d012-approval",
        collector_workflow_id="collector-graph",
        collector_deployment_ref="d012-collector",
        parent_workflow_id="parent-graph",
        parent_graph_version_ref="parent-graph@1",
        parent_deployment_ref="d012-parent",
        parent_deployment_version=1,
        payload={"request": "d012-provider-free"},
    )
    reads = 0

    def request(method: str, path: str, payload: dict[str, Any] | None = None) -> _Response:
        nonlocal reads
        if (method, path) == ("POST", "/v1/runs"):
            return _Response(202, {"run_id": "parent-run"})
        if (method, path) == ("GET", "/v1/runs/parent-run"):
            reads += 1
            return _Response(
                200,
                {
                    "run_id": "parent-run",
                    "status": "running" if reads == 1 else "paused_for_approval",
                    "deployment_ref": "d012-parent",
                    "graph_version_ref": "parent-graph@1",
                },
            )
        if method == "GET" and path.startswith("/v1/deployments/d012-parent/approvals"):
            return _Response(
                200,
                [
                    {
                        "approval_id": "approval-id",
                        "run_id": "approval-child-run",
                        "deployment_ref": "d012-approval",
                        "graph_version_ref": "approval-graph:v1",
                        "status": "pending",
                    }
                ],
            )
        if (method, path) == ("GET", "/v1/runs/parent-run/children"):
            return _Response(
                200,
                [
                    {
                        "run_id": "durable-child-run",
                        "deployment_ref": "d012-durable",
                        "status": "succeeded",
                    },
                    {
                        "run_id": "approval-child-run",
                        "deployment_ref": "d012-approval",
                        "status": "paused_for_approval",
                    },
                ],
            )
        raise AssertionError((method, path, payload))

    staged = stage_pending_child_approval(
        request=request,
        fixture=fixture,
        container_started_at="2026-08-26T05:00:00Z",
        timeout_seconds=1,
    )

    assert staged == StagedChildApproval(
        parent_run_id="parent-run",
        approval_id="approval-id",
        approval_child_run_id="approval-child-run",
        durable_child_run_id="durable-child-run",
        container_started_at="2026-08-26T05:00:00Z",
    )


def test_bounded_ui_runner_requires_changed_restart_identity_and_parses_safe_summary(
    tmp_path: Path, monkeypatch
) -> None:
    frontend = tmp_path / "frontend"
    spec = frontend / "e2e/child-approval-live.spec.ts"
    spec.parent.mkdir(parents=True)
    spec.write_text("// fixed spec\n")
    fixture = ProviderFreeChildApprovalFixture(
        schema_version=1,
        fixture_id="d012-live",
        durable_workflow_id="durable-graph",
        durable_deployment_ref="d012-durable",
        approval_workflow_id="approval-graph",
        approval_deployment_ref="d012-approval",
        collector_workflow_id="collector-graph",
        collector_deployment_ref="d012-collector",
        parent_workflow_id="parent-graph",
        parent_graph_version_ref="parent-graph@1",
        parent_deployment_ref="d012-parent",
        parent_deployment_version=1,
        payload={"request": "d012-provider-free"},
    )
    staged = StagedChildApproval(
        parent_run_id="parent-a",
        approval_id="approval-a",
        approval_child_run_id="child-a",
        durable_child_run_id="durable-a",
        container_started_at="before",
    )
    summary = {
        "schema_version": 1,
        "provider_calls_performed": 0,
        "provider_economics_status": "blocked",
        "restart_count": 1,
        "approvals": [
            {
                "decision": decision,
                "reason": f"{decision} reason",
                "approval_id": f"approval-{decision}",
                "child_run_id": f"child-{decision}",
                "parent_run_id": f"parent-{decision}",
                "parent_status": "succeeded" if decision == "approve" else "failed",
                "durable_sibling_delivery_count_before": 1,
                "durable_sibling_delivery_count_after": 1,
                "continuation_audit_count": 1,
                "signed_audit": True,
                "priced_call_count": 0,
                "total_cost_usd": 0,
                "restored_after_refresh": True,
                "restored_after_restart": True,
            }
            for decision in ("approve", "reject")
        ],
    }
    report = {
        "suites": [
            {
                "title": "provider-free child approval persistence",
                "specs": [
                    {
                        "title": "resolves exact child approve and reject after one coordinated restart",
                        "tests": [
                            {
                                "results": [
                                    {
                                        "status": "passed",
                                        "attachments": [
                                            {
                                                "name": "child-approval-live-summary",
                                                "body": base64.b64encode(
                                                    json.dumps(summary).encode()
                                                ).decode(),
                                            }
                                        ],
                                    }
                                ]
                            }
                        ],
                    }
                ],
            }
        ]
    }
    captured: dict[str, object] = {}

    def run(argv, **kwargs):
        captured.update({"argv": argv, **kwargs})
        return subprocess.CompletedProcess(argv, 0, json.dumps(report), "")

    monkeypatch.setattr(subprocess, "run", run)
    runner = BoundedChildApprovalUiRunner(
        frontend_root=frontend,
        environment={
            "ZEROTH_EVALUATION_API_KEY": "service-key",
            "ZEROTH_EVALUATION_BROWSER_ROOT": str(tmp_path / "external-evidence"),
            "OPENAI_API_KEY": "must-not-reach-child",
        },
    )

    result = runner.run(fixture, staged=staged, container_started_at_after="after")

    assert result["provider_calls_performed"] == 0
    assert result["raw_summary"] == summary
    assert result["validated_summary"]["parent_run_ids"] == [
        "parent-approve",
        "parent-reject",
    ]
    assert (tmp_path / "external-evidence/results.json").is_file()
    assert (
        tmp_path / "external-evidence/console/raw-playwright-summary.json"
    ).is_file()
    assert captured["argv"] == (
        "npm",
        "exec",
        "--",
        "playwright",
        "test",
        "e2e/child-approval-live.spec.ts",
        "--project=desktop-1440",
        "--grep",
        "resolves exact child approve and reject after one coordinated restart",
        "--reporter=json",
    )
    assert "OPENAI_API_KEY" not in captured["env"]
    assert captured["env"]["ZEROTH_EVALUATION_D012_PRE_RESTART_PARENT_RUN_ID"] == "parent-a"


def test_closed_snapshots_prove_partial_delivery_no_replay_signed_linkage_and_zero_cost(
    tmp_path: Path,
) -> None:
    before = tmp_path / "before.sqlite3"
    after = tmp_path / "after.sqlite3"

    def create(path: Path, *, terminal: bool) -> None:
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE runs (
                tenant_id TEXT, run_id TEXT, parent_run_id TEXT,
                deployment_ref TEXT, status TEXT, execution_history TEXT
            );
            CREATE TABLE token_engine_snapshots (
                tenant_id TEXT, run_id TEXT, snapshot_json TEXT
            );
            CREATE TABLE approvals (
                tenant_id TEXT, approval_id TEXT, run_id TEXT, record_json TEXT
            );
            CREATE TABLE node_audits (
                audit_id TEXT, tenant_id TEXT, run_id TEXT, deployment_ref TEXT,
                cost_usd REAL, cost_event_id TEXT, record_json TEXT
            );
            """
        )
        rows = [
            ("evaluation-studio-v1", "parent-approve", None, "d012-parent", "COMPLETED" if terminal else "WAITING_APPROVAL", "[]"),
            ("evaluation-studio-v1", "durable-approve", "parent-approve", "d012-durable", "COMPLETED", json.dumps([{"node_id": "subgraph:d012-durable:1:durable"}])),
            ("evaluation-studio-v1", "child-approve", "parent-approve", "d012-approval", "COMPLETED" if terminal else "WAITING_APPROVAL", json.dumps([{"node_id": "subgraph:d012-approval:1:approve"}])),
        ]
        if terminal:
            rows.extend([
                ("evaluation-studio-v1", "collector-approve", "parent-approve", "d012-collector", "COMPLETED", "[]"),
                ("evaluation-studio-v1", "parent-reject", None, "d012-parent", "FAILED", "[]"),
                ("evaluation-studio-v1", "durable-reject", "parent-reject", "d012-durable", "COMPLETED", json.dumps([{"node_id": "subgraph:d012-durable:1:durable"}])),
                ("evaluation-studio-v1", "child-reject", "parent-reject", "d012-approval", "FAILED", json.dumps([{"node_id": "subgraph:d012-approval:1:approve"}])),
            ])
        connection.executemany("INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?)", rows)
        connection.execute(
            "INSERT INTO token_engine_snapshots VALUES (?, ?, ?)",
            (
                "evaluation-studio-v1",
                "parent-approve",
                json.dumps({
                    "state": "completed" if terminal else "running",
                    "joins": [] if terminal else [{
                        "target_node_id": "collector",
                        "obligations": [
                            {"delivery": {"payload": {"branch": "durable"}}},
                            {"delivery": None},
                        ],
                    }],
                }),
            ),
        )
        approvals = [
            {
                "approval_id": "approval-approve",
                "run_id": "child-approve",
                "deployment_ref": "d012-approval",
                "status": "resolved" if terminal else "pending",
                "resolution": ({"decision": "approve", "reason": "approve reason"} if terminal else None),
            }
        ]
        if terminal:
            approvals.append({
                "approval_id": "approval-reject",
                "run_id": "child-reject",
                "deployment_ref": "d012-approval",
                "status": "resolved",
                "resolution": {"decision": "reject", "reason": "reject reason"},
            })
        connection.executemany(
            "INSERT INTO approvals VALUES (?, ?, ?, ?)",
            [
                ("evaluation-studio-v1", record["approval_id"], record["run_id"], json.dumps(record))
                for record in approvals
            ],
        )
        if terminal:
            for decision in ("approve", "reject"):
                parent = f"parent-{decision}"
                child = f"child-{decision}"
                approval_id = f"approval-{decision}"
                audit = {
                    "audit_id": (
                        f"{parent}:child-approval-continuation:{approval_id}"
                    ),
                    "run_id": parent,
                    "deployment_ref": "d012-parent",
                    "status": "child_approval_continuation_scheduled",
                    "record_signature": f"hmac-sha256:{decision}",
                    "cost_usd": 0,
                    "estimated_cost_usd": 0,
                    "cost_event_id": None,
                    "token_usage": None,
                    "execution_metadata": {
                        "child_run_id": child,
                        "continuation_parent_run_id": parent,
                    },
                }
                connection.execute(
                    "INSERT INTO node_audits VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        audit["audit_id"],
                        "evaluation-studio-v1",
                        parent,
                        "d012-parent",
                        0,
                        None,
                        json.dumps(audit),
                    ),
                )
        connection.commit()
        connection.close()

    create(before, terminal=False)
    create(after, terminal=True)
    fixture = ProviderFreeChildApprovalFixture(
        schema_version=1,
        fixture_id="d012-live",
        durable_workflow_id="durable-graph",
        durable_deployment_ref="d012-durable",
        approval_workflow_id="approval-graph",
        approval_deployment_ref="d012-approval",
        collector_workflow_id="collector-graph",
        collector_deployment_ref="d012-collector",
        parent_workflow_id="parent-graph",
        parent_graph_version_ref="parent-graph@1",
        parent_deployment_ref="d012-parent",
        parent_deployment_version=1,
        payload={"request": "d012-provider-free"},
    )
    staged = StagedChildApproval(
        parent_run_id="parent-approve",
        approval_id="approval-approve",
        approval_child_run_id="child-approve",
        durable_child_run_id="durable-approve",
        container_started_at="before",
    )
    summary = {
        "schema_version": 1,
        "provider_calls_performed": 0,
        "provider_economics_status": "blocked",
        "restart_count": 1,
        "approvals": [
            {
                "decision": decision,
                "reason": f"{decision} reason",
                "approval_id": f"approval-{decision}",
                "child_run_id": f"child-{decision}",
                "parent_run_id": f"parent-{decision}",
                "parent_status": "succeeded" if decision == "approve" else "failed",
                "durable_sibling_delivery_count_before": 1,
                "durable_sibling_delivery_count_after": 1,
                "continuation_audit_count": 1,
                "signed_audit": True,
                "priced_call_count": 0,
                "total_cost_usd": 0,
                "restored_after_refresh": True,
                "restored_after_restart": True,
            }
            for decision in ("approve", "reject")
        ],
    }

    result = validate_child_approval_snapshots(
        before,
        after,
        tenant_id="evaluation-studio-v1",
        fixture=fixture,
        staged=staged,
        summary=summary,
    )

    assert result == {
        "partial_delivery_count_before_restart": 1,
        "durable_sibling_replay_count": 0,
        "signed_continuation_count": 2,
        "priced_call_count": 0,
        "total_cost_usd": 0.0,
        "provider_economics_status": "blocked",
    }
