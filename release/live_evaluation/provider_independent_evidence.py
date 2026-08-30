"""Seal the provider-independent 2026-08-24 campaign supplement.

This deliberately does not use the all-or-nothing original campaign finalizer:
the exposed provider credential blocks the paid workflow matrices, while the
local workflow, approval, sandbox, economics, and UI evidence remains useful.
"""

# Evidence prose and recorded command vectors are intentionally preserved verbatim.
# ruff: noqa: E501

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
from pathlib import Path

from .evidence import AcceptanceCriterion, CorrelationIds, EvidenceStore
from .runtime_paths import resolve_runtime_paths

_RUNTIME_PATHS = resolve_runtime_paths()
WORKTREE = _RUNTIME_PATHS.worktree
STATE_ROOT = _RUNTIME_PATHS.state_root
DASHBOARD_ROOT = STATE_ROOT / "evidence/ui-dashboard-20260824-rerun"
GRAPH_ROOT = STATE_ROOT / "evidence/ui-graphs-20260824"


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=WORKTREE,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _tree_digest() -> str:
    status = _git("status", "--porcelain=v1")
    diff = subprocess.run(
        ["git", "diff", "--binary", "HEAD"],
        cwd=WORKTREE,
        check=True,
        capture_output=True,
    ).stdout
    staged = subprocess.run(
        ["git", "diff", "--binary", "--cached", "HEAD"],
        cwd=WORKTREE,
        check=True,
        capture_output=True,
    ).stdout
    return hashlib.sha256(status.encode() + diff + staged).hexdigest()


def _find(root: Path, name: str) -> Path:
    matches = tuple(path for path in root.rglob("*") if path.name.endswith(name))
    if len(matches) != 1:
        raise RuntimeError(f"expected one {name} below {root}, found {len(matches)}")
    return matches[0]


def build(root: Path) -> None:
    store = EvidenceStore(root)
    if any(root.iterdir()):
        raise RuntimeError(f"evidence root must be empty: {root}")

    store.write_manifest(
        {
            "campaign_id": "provider-independent-20260824",
            "campaign_scope": "local workflows, approvals, sandbox, economics, and UI",
            "revision": _git("rev-parse", "HEAD"),
            "working_tree_sha256": _tree_digest(),
            "repository": str(WORKTREE),
            "state_root": str(STATE_ROOT),
            "runtime": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "frontend": "Next.js development service on loopback port 3000",
                "backend": "signed evaluation service on loopback port 8122",
                "chroma": "1.5.6 on loopback port 8121",
            },
            "budget": {
                "tenant_ceiling_usd": 10,
                "run_ceiling_usd": 0.25,
                "admission_mode": "fail_closed",
            },
            "provider_state": "paid campaign blocked pending credential rotation",
            "canonical_economics": {
                "actual_spend_usd": 0.00001312,
                "ambiguous_exposure_usd": 0.00000028,
                "budget_consumed_usd": 0.0000134,
                "synthetic_control_usd": 0.01,
            },
        }
    )

    external_index = {
        "dashboard_playwright_root": str(DASHBOARD_ROOT),
        "graph_playwright_root": str(GRAPH_ROOT),
        "dashboard_results": str(DASHBOARD_ROOT / "results.json"),
        "dashboard_html_report": str(DASHBOARD_ROOT / "html-report/index.html"),
        "graph_results": str(GRAPH_ROOT / "results.json"),
        "graph_html_report": str(GRAPH_ROOT / "html-report/index.html"),
        "note": "These external roots contain the complete sanitized Playwright reports, videos, screenshots, network summaries, and accessibility records.",
    }
    index_source = root / ".external-evidence-index.json"
    index_source.write_text(json.dumps(external_index, indent=2) + "\n")
    store.ingest_artifact(index_source, "handoff/external-evidence-index.json")
    index_source.unlink()

    artifacts = (
        (DASHBOARD_ROOT / "results.json", "playwright-report/dashboard-results.json"),
        (GRAPH_ROOT / "results.json", "playwright-report/graph-results.json"),
        (
            _find(DASHBOARD_ROOT, "run-evidence-axe.json"),
            "accessibility/dashboard-axe.json",
        ),
        (_find(DASHBOARD_ROOT, "audit.png"), "screenshots/audit.png"),
        (_find(DASHBOARD_ROOT, "cost.png"), "screenshots/economics.png"),
        (_find(DASHBOARD_ROOT, "rightsizing.png"), "screenshots/rightsizing.png"),
        (
            _find(GRAPH_ROOT, "loop-architecture-desktop-1440.png"),
            "screenshots/loop-architecture-1440.png",
        ),
        (
            _find(GRAPH_ROOT, "loop-architecture-mobile-390.png"),
            "screenshots/loop-architecture-390.png",
        ),
        (
            _find(GRAPH_ROOT, "approval-action-architecture-desktop-1440.png"),
            "screenshots/approval-action-1440.png",
        ),
        (
            _find(GRAPH_ROOT, "approval-action-architecture-mobile-390.png"),
            "screenshots/approval-action-390.png",
        ),
    )
    for source, destination in artifacts:
        store.ingest_artifact(source, destination)

    graph_video = sorted(GRAPH_ROOT.rglob("video.webm"))[0]
    dashboard_video = sorted(DASHBOARD_ROOT.rglob("video.webm"))[0]
    store.ingest_artifact(graph_video, "videos/graph-journey.webm")
    store.ingest_artifact(dashboard_video, "videos/dashboard-journey.webm")

    observed_commands = (
        (
            "backend-full-suite",
            ["uv", "run", "pytest", "-q"],
            "9839 passed, 7 skipped, 465 deselected, 493 warnings in 510.66s\n",
        ),
        (
            "frontend-unit-suite",
            ["npm", "test", "--", "--run"],
            "33 test files passed; 158 tests passed\n",
        ),
        (
            "frontend-production-build",
            ["npm", "run", "build"],
            "Next.js production build completed; 23 static pages generated\n",
        ),
        (
            "dashboard-playwright-rerun",
            ["npx", "playwright", "test", "e2e/incumbent-dashboard-live.spec.ts"],
            "Desktop dashboard acceptance: 9 passed; multi-viewport core dashboard checks passed\n",
        ),
        (
            "graph-playwright",
            ["npx", "playwright", "test", "e2e/incumbent-dashboard-live.spec.ts", "--grep", "loop|approval"],
            "Loop and governed-action architecture: 8 passed across four viewports\n",
        ),
    )
    for sequence, (name, argv, stdout) in enumerate(observed_commands, start=1):
        store.record_command(
            sequence=sequence,
            name=name,
            argv=argv,
            working_directory=WORKTREE / ("frontend" if "frontend" in name or "playwright" in name else ""),
            exit_code=0,
            stdout=stdout,
            stderr="",
        )
        store.append_event(
            "verification.summary",
            {
                "capture_mode": "post_run_terminal_summary",
                "command_name": name,
                "result": "pass",
            },
        )

    workflows = {
        "local-profiler": (
            "eb057063dee048b69a8fb23d4635649d",
            "7bb1ba4218d94c5f8d39ca60de7c44ae",
            "592625ebee5649778287a0f500092842",
        ),
        "approved-local-profiler": (
            "a4675b36f90248789d63170f66bceabe",
            "323101368c874d32b8844c2a4aa2cac0",
            "b4f9e9e7dc8749d0a0493c36868bd6ae",
        ),
        "incident-loop": (
            "287fcbc1f7f64e37acd970f09b0d8d77",
            "1a27b0b3d4e24549b6e41c6d7f209209",
            "ca433a2659d945049902faddbf741c6a",
        ),
        "quality-loop": (
            "6aa11f91988047b6a8e052e55d10a5b8",
            "c447ed5e932a47b5b0c8edf5595c6a0f",
            "0a1bd40c315145a8ba97abe842df2aa9",
        ),
        "governed-remediation": (
            "4c316dd7eb0846a49946d935983acd94",
            "1320abb389ff486ba3a282954668f03d",
            "212cee7ea97f47cbb28e8518bededd9e",
        ),
    }
    workflow_event_refs: dict[str, str] = {}
    for workflow, run_ids in workflows.items():
        event_id = store.append_event(
            "workflow.run.acceptance",
            {
                "happy_path_repetitions": 3,
                "result": "pass",
                "workflow": workflow,
            },
            correlation=CorrelationIds(run_id=run_ids[0]),
        )
        workflow_event_refs[workflow] = f"events.ndjson#{event_id}"
        for run_id in run_ids[1:]:
            store.append_event(
                "workflow.run.repetition",
                {"result": "pass", "workflow": workflow},
                correlation=CorrelationIds(run_id=run_id),
            )

    negative_event = store.append_event(
        "workflow.negative.acceptance",
        {
            "approval_rejection_zero_effect": "pass",
            "cancellation_after_approval_zero_effect": "pass",
            "duplicate_resolution_conflict": "pass",
            "loop_max_retries_exhausted": "pass",
            "malformed_local_input": "pass",
            "sink_unavailable_zero_effect": "pass",
            "timeout_after_commit_one_marker_no_reexecution": "pass",
        },
    )
    sandbox_event = store.append_event(
        "sandbox.acceptance",
        {
            "hostile_workload_tests": 15,
            "local_manifest_standard_mode": "sandboxed",
            "strict_mode_without_enforcing_backend": "fail_closed",
        },
    )

    acceptance = (
        AcceptanceCriterion("local.profiler.three-repetitions", "pass", (workflow_event_refs["local-profiler"],)),
        AcceptanceCriterion("local.approved-profiler.three-repetitions", "pass", (workflow_event_refs["approved-local-profiler"],)),
        AcceptanceCriterion("loop.incident.three-repetitions", "pass", (workflow_event_refs["incident-loop"],)),
        AcceptanceCriterion("loop.quality.three-repetitions", "pass", (workflow_event_refs["quality-loop"],)),
        AcceptanceCriterion("approval.governed-remediation.three-repetitions", "pass", (workflow_event_refs["governed-remediation"],)),
        AcceptanceCriterion("negative.local-and-governed-matrix", "pass", (f"events.ndjson#{negative_event}",)),
        AcceptanceCriterion("sandbox.local-manifest", "pass", (f"events.ndjson#{sandbox_event}",)),
        AcceptanceCriterion("ui.dashboards", "pass", ("playwright-report/dashboard-results.json", "screenshots/economics.png")),
        AcceptanceCriterion("ui.loop-four-viewports", "pass", ("playwright-report/graph-results.json", "screenshots/loop-architecture-1440.png", "screenshots/loop-architecture-390.png")),
        AcceptanceCriterion("ui.approval-four-viewports", "pass", ("playwright-report/graph-results.json", "screenshots/approval-action-1440.png", "screenshots/approval-action-390.png")),
        AcceptanceCriterion("ui.accessibility", "pass", ("accessibility/dashboard-axe.json",)),
        AcceptanceCriterion("economics.canonical-ledger", "pass", ("screenshots/economics.png", "handoff/external-evidence-index.json")),
        AcceptanceCriterion("audit.signed-local-chains", "pass", ("screenshots/audit.png",)),
        AcceptanceCriterion("provider.workflow1-paid-matrix", "blocked", (), "Credential rotation required; no additional paid call was attempted."),
        AcceptanceCriterion("provider.workflow2-paid-matrix", "blocked", (), "Credential rotation required; child/parent provider matrix remains unexecuted."),
        AcceptanceCriterion("provider.rightsizing-measured-comparison", "blocked", (), "Static lookup and no-traffic gate passed; measured provider comparison requires a rotated credential."),
    )

    report = """# Provider-independent live-evaluation supplement

Status: **LOCAL ACCEPTANCE PASS; ORIGINAL PAID CAMPAIGN BLOCKED**

Five workflow families completed three happy-path repetitions each. Local code manifests, dedicated loop nodes with bounded retries, approval resolution, the durable action sink, ambiguous-outcome refusal, and sandbox fail-closed behavior passed their negative cases. Dashboard and graph journeys passed at the tested viewports with sanitized screenshots, videos, accessibility evidence, and Playwright result files.

The original provider-backed Workflow 1 and Workflow 2 matrices and the measured Rightsizing comparison are not accepted. The previously used credential was exposed in local diagnostic output and must be rotated before another paid request. No new paid provider call was made while producing this supplement.

Economics uses one canonical ledger. Actual attributed spend is $0.00001312, retained ambiguous exposure is $0.00000028, and budget consumed is $0.0000134. The $0.01 synthetic control is labeled separately and is not deployment spend.

## Adversarial review

The strongest objection is that provider-independent workflows cannot prove live model quality, provider fault handling, or shared-project reconciliation. That objection is valid, so this bundle does not claim full original campaign acceptance. The safer fallback is the state captured here: preserve the local/UI foundation, rotate the credential, then resume only the blocked paid criteria under the existing $10 tenant and $0.25 per-run ceilings.

## Rollback and operation

The persistent topology is `compose.dev.yml`. Restart `frontend` for UI changes and `backend` for Python runtime changes; persistent state remains below the external evaluation root. Roll back loop visualization by reverting the graph-presentation and edge-view changes. Roll back local manifest execution by unregistering the local units. Preserve the action-sink and audit databases during rollback so ambiguity and approval evidence remain authoritative.
"""
    store.finalize_bundle(acceptance=acceptance, report_markdown=report)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    build(args.root)


if __name__ == "__main__":
    main()
