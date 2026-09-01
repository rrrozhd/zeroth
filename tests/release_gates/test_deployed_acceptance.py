from __future__ import annotations

import json
from pathlib import Path

import yaml

from release.acceptance.models import REQUIRED_SCENARIOS, AcceptanceContract

ROOT = Path(__file__).resolve().parents[2]


def _workflow(path: Path) -> dict:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    value["on"] = value.pop(True, value.get("on"))
    return value


def _script(job: dict) -> str:
    return "\n".join(step.get("run", "") for step in job["steps"] if isinstance(step, dict))


def test_product_contract_is_complete_and_fail_closed() -> None:
    path = ROOT / "release/acceptance/contracts/zeroth-v1.json"
    contract = AcceptanceContract.model_validate(json.loads(path.read_text(encoding="utf-8")))

    assert set(contract.scenarios) == set(REQUIRED_SCENARIOS)
    assert contract.cleanup
    assert all(step.resource_id for step in contract.cleanup)


def test_manual_workflow_targets_a_selected_url_and_exact_release_identity() -> None:
    workflow = _workflow(ROOT / ".github/workflows/deployed-acceptance.yml")
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    job = workflow["jobs"]["deployed-acceptance"]
    script = _script(job)

    assert {"deployment_url", "tenant_id", "deployment_ref", "release_run_id"} <= set(inputs)
    assert all(inputs[name]["required"] for name in inputs)
    assert "candidate-identity-full" in script
    assert "gh run download" in script
    assert "python -m release.acceptance.cli" in script
    assert "continue-on-error" in str(job["steps"])
    assert "steps.acceptance.outcome != 'success'" in str(job["steps"])


def test_deployed_acceptance_marker_is_deselected_by_default() -> None:
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "deployed_acceptance:" in project
    assert "not deployed_acceptance" in project
