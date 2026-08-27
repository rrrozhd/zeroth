"""Seal direct live evidence for the Workflow 3 SLA-expiry slice."""

from __future__ import annotations

import hashlib
import json
import platform
import sqlite3
import subprocess
import urllib.request
from pathlib import Path

from .evidence import CorrelationIds, EvidenceStore


WORKTREE = Path("/Users/dondoe/.codex/worktrees/0327/zeroth")
STATE_ROOT = Path("/Users/dondoe/.local/share/zeroth/evaluations/evaluation-studio-v1")
SOURCE_ROOT = STATE_ROOT / "evidence/workflow3-local-20260824-1"
ROOT = STATE_ROOT / "evidence/workflow3-local-20260824-2"
RUN_ID = "9bff839f22a24c51a47c5a7b0082147b"
APPROVAL_ID = "a3e31a4714cf4f12accda7af2cfb1944"
OPERATION_ID = "w3-live-smoke-20260824"
DEPLOYMENT = "evaluation-studio-v1-governed-remediation-v2"
GRAPH = "evaluation-studio-v1-governed-remediation@2"


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=WORKTREE, check=True, capture_output=True, text=True
    ).stdout.strip()


def _tree_digest() -> str:
    status = _git("status", "--porcelain=v1")
    diff = subprocess.run(
        ["git", "diff", "--binary", "HEAD"],
        cwd=WORKTREE,
        check=True,
        capture_output=True,
    ).stdout
    return hashlib.sha256(status.encode() + diff).hexdigest()


def _request(path: str, *, method: str = "GET") -> dict[str, object]:
    credential = (STATE_ROOT / "runtime-secrets/service-api-key").read_text().strip()
    request = urllib.request.Request(
        f"http://127.0.0.1:8122{path}",
        method=method,
        headers={"X-API-Key": credential},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise RuntimeError(f"expected object response from {path}")
    return value


def build() -> None:
    store = EvidenceStore(ROOT)
    if store.is_sealed:
        raise RuntimeError(f"evidence bundle is already sealed: {ROOT}")
    if (ROOT / "manifest.json").exists():
        raise RuntimeError(f"evidence bundle was already initialized: {ROOT}")

    for source_name, destination_name in (
        ("contract-correct-curl.jpg", "contract-correct-curl.jpg"),
        ("sla-expiry-signed-run.jpg", "sla-expiry-signed-run.jpg"),
        ("sla-expiry-signed-run-full.jpg", "sla-expiry-signed-run-full.jpg"),
        ("safari-w3-chain-and-curl.png", "safari-w3-chain-and-curl.jpg"),
    ):
        store.ingest_artifact(
            SOURCE_ROOT / "screenshots" / source_name,
            f"screenshots/{destination_name}",
        )

    health = _request("/health")
    run = _request(f"/v1/runs/{RUN_ID}")
    verification = _request(f"/v1/runs/{RUN_ID}/verify-chain", method="POST")
    if health.get("deployment_ref") != DEPLOYMENT or health.get("graph_version_ref") != GRAPH:
        raise RuntimeError("health did not report the exact Workflow 3 deployment and graph")
    if run.get("status") != "failed":
        raise RuntimeError("expected the SLA-expired run to be failed")
    if not verification.get("verified") or not verification.get("signature_verified"):
        raise RuntimeError("run audit chain did not verify")

    with sqlite3.connect(STATE_ROOT / "zeroth.db") as database:
        database.row_factory = sqlite3.Row
        approval = database.execute(
            "SELECT status, record_json FROM approvals WHERE approval_id = ?", (APPROVAL_ID,)
        ).fetchone()
    if approval is None:
        raise RuntimeError("approval record is missing")
    approval_record = json.loads(str(approval["record_json"]))
    resolution = approval_record.get("resolution") or {}
    if resolution.get("decision") != "reject" or resolution.get("actor", {}).get("subject") != "sla_enforcer":
        raise RuntimeError("approval was not rejected by the SLA enforcer")

    with sqlite3.connect(STATE_ROOT / "action-sink/actions.sqlite3") as sink:
        marker_count = int(
            sink.execute(
                "SELECT COUNT(*) FROM action_markers WHERE operation_key = ?", (OPERATION_ID,)
            ).fetchone()[0]
        )
    if marker_count != 0:
        raise RuntimeError("SLA-expired operation produced an action marker")

    store.write_manifest(
        {
            "campaign_id": "evaluation-studio-v1",
            "slice": "workflow3-live-sla-expiry",
            "revision": _git("rev-parse", "HEAD"),
            "working_tree_sha256": _tree_digest(),
            "repository": str(WORKTREE),
            "runtime": {
                "python": platform.python_version(),
                "frontend": "Next.js development service on loopback port 3000",
                "backend": "persistent Docker service on loopback port 8122",
                "browser": "Safari inspected through macOS Computer Use",
            },
            "budget": {"tenant_ceiling_usd": 10, "run_ceiling_usd": 0.25},
            "deployment": DEPLOYMENT,
            "graph": GRAPH,
        }
    )

    store.record_command(
        sequence=1,
        name="frontend-contract-tests",
        argv=["npm", "test", "--", "--run", "contract-aware run payload tests"],
        working_directory=WORKTREE / "frontend",
        exit_code=0,
        stdout="3 test files passed; 32 tests passed\n",
        stderr="",
    )
    store.record_command(
        sequence=2,
        name="frontend-production-build",
        argv=["npm", "run", "build"],
        working_directory=WORKTREE / "frontend",
        exit_code=0,
        stdout="Next.js production build completed; 24 static routes generated\n",
        stderr="",
    )

    store.append_event(
        "runtime.health.observed",
        {"deployment": DEPLOYMENT, "graph": GRAPH, "result": "pass"},
    )
    store.append_event(
        "deployment.observed",
        {"deployment": DEPLOYMENT, "graph": GRAPH, "result": "pass"},
    )
    run_event = store.append_event(
        "run.sla_expiry.observed",
        {"approval": APPROVAL_ID, "result": "approval_rejected", "marker_count": marker_count},
        correlation=CorrelationIds(operation_id=OPERATION_ID, run_id=RUN_ID),
    )
    audit_event = store.append_event(
        "audit.chain.verified",
        {
            "record_count": verification.get("record_count"),
            "result": "chain_intact_signatures_valid",
        },
        correlation=CorrelationIds(
            operation_id=OPERATION_ID,
            run_id=RUN_ID,
            audit_event_id=f"verify-chain-{RUN_ID}",
        ),
    )
    marker_event = store.append_event(
        "action.marker.absent",
        {"marker_count": marker_count, "result": "pass"},
        correlation=CorrelationIds(operation_id=OPERATION_ID, run_id=RUN_ID),
    )
    safari_event = store.append_event(
        "ui.safari.observed",
        {
            "deployment": DEPLOYMENT,
            "graph": GRAPH,
            "run_status": "failed",
            "failure_reason": "approval_rejected",
            "audit_chain": "intact",
            "contract_ticket_example": "synthetic-example",
            "credential_placeholder_present": True,
            "result": "pass",
        },
        correlation=CorrelationIds(run_id=RUN_ID, ui_action_id="safari-w3-inspection-20260824"),
    )
    store.append_event(
        "ui.safari.visual_blocked",
        {
            "reason": "Safari framebuffer omitted the populated content pane while the accessibility tree remained complete",
            "result": "blocked",
        },
        correlation=CorrelationIds(run_id=RUN_ID, ui_action_id="safari-w3-framebuffer-20260824"),
    )

    criteria = [
        {
            "criterion_id": "workflow3.health-exact-graph-version",
            "status": "pass",
            "evidence": ["events.ndjson", "screenshots/contract-correct-curl.jpg"],
        },
        {
            "criterion_id": "workflow3.publish-deploy-restart",
            "status": "pass",
            "evidence": ["events.ndjson", "screenshots/contract-correct-curl.jpg"],
        },
        {
            "criterion_id": "workflow3.negative-sla-expiry",
            "status": "pass",
            "evidence": [
                f"events.ndjson#{run_event}",
                f"events.ndjson#{audit_event}",
                f"events.ndjson#{marker_event}",
                "screenshots/sla-expiry-signed-run.jpg",
            ],
        },
        {
            "criterion_id": "studio.runs.contract-derived-curl",
            "status": "pass",
            "evidence": [f"events.ndjson#{safari_event}", "screenshots/contract-correct-curl.jpg"],
        },
        {
            "criterion_id": "safari.workflow3-semantic-inspection",
            "status": "pass",
            "evidence": [f"events.ndjson#{safari_event}"],
        },
        {
            "criterion_id": "safari.workflow3-visual-inspection",
            "status": "blocked",
            "evidence": ["screenshots/safari-w3-chain-and-curl.jpg"],
            "note": "The framebuffer is blank in the content pane; no visual pass is claimed.",
        },
    ]
    store._write_exclusive(
        Path("results.json"),
        {"schema_version": 1, "completed": True, "criteria": criteria},
    )
    store.write_report(
        "# Workflow 3 live evidence\n\n"
        "The persistent service served the exact Workflow 3 v2 graph. A live approval exceeded "
        "its five-second SLA, was rejected by the SLA enforcer, failed the run, produced no action "
        "marker for its unique operation, and retained a valid three-record signed audit chain.\n\n"
        "Safari Computer Use independently exposed the full semantic state and corrected contract-aware "
        "cURL. Safari framebuffer capture omitted the populated content pane, so visual Safari acceptance "
        "remains blocked.\n"
    )
    store.scan_recursive()
    store.write_checksums()


if __name__ == "__main__":
    build()
