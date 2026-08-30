from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).parents[3]
WORKFLOW = ROOT / ".github/workflows/ci.yml"


def test_action_pilot_runs_the_real_composite_action_with_read_only_permissions() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["zeroth-check-action"]
    assert job["permissions"] == {"contents": "read"}
    uses = [step.get("uses", "") for step in job["steps"]]
    assert "./" in uses
    assert all("@v" not in action for action in uses if action.startswith("actions/"))

    action_step = next(step for step in job["steps"] if step.get("uses") == "./")
    assert action_step["with"] == {
        "config": "apps/check_payment/zeroth-check.yaml",
        "report-dir": ".zeroth/check/reports",
        "fail-on": "block,invalid",
    }


def test_action_pilot_persists_reports_but_never_recordings() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["zeroth-check-action"]["steps"]
    upload = next(step for step in steps if "actions/upload-artifact@" in step.get("uses", ""))

    assert upload["if"] == "${{ always() }}"
    assert upload["with"]["path"] == ".zeroth/check/reports"
    assert "recordings" not in WORKFLOW.read_text(encoding="utf-8")
