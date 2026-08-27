from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from release.live_evaluation.workflow3_lifecycle_evidence import (
    APPROVAL_ID,
    DEPLOYMENT,
    GRAPH,
    RUN_ID,
    _acceptance_criteria,
    _tree_digest,
    _validate_observations,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_tree_digest_binds_untracked_file_contents(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "evidence@example.test")
    _git(tmp_path, "config", "user.name", "Evidence Test")
    (tmp_path / "tracked.txt").write_text("tracked\n")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "-m", "fixture")
    untracked = tmp_path / "collector.py"
    untracked.write_text("first\n")

    before = _tree_digest(tmp_path)
    untracked.write_text("second\n")

    assert _tree_digest(tmp_path) != before


def test_validate_observations_requires_direct_sla_and_identity_joins() -> None:
    workflow = {"id": "evaluation-studio-v1-governed-remediation", "status": "published", "version": 3}
    deployment = {
        "deployment_ref": DEPLOYMENT,
        "version": 2,
        "graph_version_ref": GRAPH,
        "status": "active",
        "serving": True,
    }
    health = {
        "status": "ok",
        "deployment_ref": DEPLOYMENT,
        "deployment_version": 2,
        "graph_version_ref": GRAPH,
    }
    run = {
        "run_id": RUN_ID,
        "thread_id": RUN_ID,
        "status": "failed",
        "deployment_ref": DEPLOYMENT,
        "graph_version_ref": GRAPH,
        "tenant_id": "evaluation-studio-v1",
        "workspace_id": None,
        "failure_state": {"reason": "approval_rejected"},
    }
    approval = {
        "approval_id": APPROVAL_ID,
        "run_id": RUN_ID,
        "thread_id": RUN_ID,
        "node_id": "approval",
        "deployment_ref": DEPLOYMENT,
        "graph_version_ref": GRAPH,
        "tenant_id": "evaluation-studio-v1",
        "workspace_id": None,
        "status": "resolved",
        "urgency_metadata": {"sla_timeout_seconds": 5},
        "created_at": "2026-08-24T20:40:42.933802Z",
        "sla_deadline": "2026-08-24T20:40:47.933802Z",
        "resolution": {
            "decision": "reject",
            "actor": {
                "subject": "sla_enforcer",
                "tenant_id": "evaluation-studio-v1",
                "workspace_id": None,
            },
            "resolved_at": "2026-08-24T20:40:52.146444Z",
        },
    }
    audits = [
        {
            "audit_id": f"audit-{sequence}",
            "run_id": RUN_ID,
            "thread_id": RUN_ID,
            "deployment_ref": DEPLOYMENT,
            "graph_version_ref": GRAPH,
            "tenant_id": "evaluation-studio-v1",
            "workspace_id": None,
            "chain_sequence": sequence,
            "record_digest": f"digest-{sequence}",
            "record_signature": f"signature-{sequence}",
            "signing_key_id": "dev-local",
            "signing_algorithm": "HS256",
        }
        for sequence in range(1, 4)
    ]
    verification = {
        "verified": True,
        "signature_verified": True,
        "record_count": 3,
        "unsigned_record_count": 0,
    }

    _validate_observations(
        workflow=workflow,
        deployment=deployment,
        health=health,
        run=run,
        approval=approval,
        audits=audits,
        verification=verification,
        side_effect_count=0,
        marker_count_since_run=0,
    )

    approval["resolution"]["resolved_at"] = "2026-08-24T20:40:46.000000Z"
    with pytest.raises(RuntimeError, match="deadline"):
        _validate_observations(
            workflow=workflow,
            deployment=deployment,
            health=health,
            run=run,
            approval=approval,
            audits=audits,
            verification=verification,
            side_effect_count=0,
            marker_count_since_run=0,
        )

    approval["resolution"]["resolved_at"] = "2026-08-24T20:40:52.146444Z"
    approval["resolution"]["actor"]["tenant_id"] = "default"
    with pytest.raises(RuntimeError, match="tenant or workspace"):
        _validate_observations(
            workflow=workflow,
            deployment=deployment,
            health=health,
            run=run,
            approval=approval,
            audits=audits,
            verification=verification,
            side_effect_count=0,
            marker_count_since_run=0,
        )

    approval["resolution"]["actor"]["tenant_id"] = "evaluation-studio-v1"
    audits[1]["tenant_id"] = "default"
    with pytest.raises(RuntimeError, match="audit tenant or workspace"):
        _validate_observations(
            workflow=workflow,
            deployment=deployment,
            health=health,
            run=run,
            approval=approval,
            audits=audits,
            verification=verification,
            side_effect_count=0,
            marker_count_since_run=0,
        )

    audits[1]["tenant_id"] = "evaluation-studio-v1"
    next_run = "b778fa14a8104527a7a0b78f5bfaef23"
    next_approval = "c97101a47aa24e6987031d7fb10a4970"
    next_graph = "evaluation-studio-v1-governed-remediation@4"
    workflow["version"] = 4
    deployment.update(version=3, graph_version_ref=next_graph)
    health.update(deployment_version=3, graph_version_ref=next_graph)
    run.update(run_id=next_run, thread_id=next_run, graph_version_ref=next_graph)
    approval.update(
        approval_id=next_approval,
        run_id=next_run,
        thread_id=next_run,
        graph_version_ref=next_graph,
    )
    for audit in audits:
        audit.update(run_id=next_run, thread_id=next_run, graph_version_ref=next_graph)

    _validate_observations(
        workflow=workflow,
        deployment=deployment,
        health=health,
        run=run,
        approval=approval,
        audits=audits,
        verification=verification,
        side_effect_count=0,
        marker_count_since_run=0,
        expected_workflow_version=4,
        expected_deployment_version=3,
        expected_graph=next_graph,
        expected_run_id=next_run,
        expected_approval_id=next_approval,
    )


def test_strict_campaign_disposition_accepts_only_exact_health() -> None:
    criteria = {item.criterion_id: item for item in _acceptance_criteria({
        "health": "console/health.json",
        "workflow": "console/workflow.json",
        "deployments": "console/deployments.json",
        "run": "console/run.json",
        "approval": "console/approval.json",
        "audits": "console/audits.json",
        "verification": "console/verification.json",
        "persistence": "console/persistence.json",
    })}

    assert criteria["workflow3.health-exact-graph-version"].status == "pass"
    assert {
        item.criterion_id
        for item in criteria.values()
        if item.status == "pass"
    } == {"workflow3.health-exact-graph-version"}
    assert criteria["ui.node-menu"].status == "not_run"
    assert criteria["ui.keyboard-shortcuts"].status == "not_run"
    assert criteria["ui.publish-deploy-run"].status == "not_run"
    assert criteria["workflow3.publish-deploy-restart"].status == "not_run"
    assert criteria["workflow3.negative-sla-expiry"].status == "not_run"


def test_v4_checkpoint_disposition_keeps_screenshot_and_economics_gates_conservative() -> None:
    from release.live_evaluation.workflow3_v4_checkpoint_evidence import (
        _checkpoint_criteria,
        _sanitize_safari_log,
    )

    criteria = {item.criterion_id: item for item in _checkpoint_criteria({"health": "console/health.json"})}

    assert {item.criterion_id for item in criteria.values() if item.status == "pass"} == {
        "workflow3.health-exact-graph-version"
    }
    assert criteria["ui.keyboard-shortcuts"].status == "not_run"
    assert criteria["ui.node-menu"].status == "not_run"
    assert criteria["ui.publish-deploy-run"].status == "not_run"
    assert criteria["workflow3.negative-sla-expiry"].status == "not_run"

    sanitized = _sanitize_safari_log(
        {
            "action": "inspect-generated-curl",
            "ax_checkpoint": 'curl -H "X-API-Key: $ZEROTH_API_KEY" http://127.0.0.1',
        }
    )
    assert sanitized["action"] == "inspect-generated-curl"
    assert sanitized["ax_checkpoint"] == "[credential header withheld]"
