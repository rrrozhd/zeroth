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


def test_fixture_contract_is_complete_and_fail_closed() -> None:
    path = ROOT / "release/acceptance/contracts/fixture-v1.json"
    contract = AcceptanceContract.model_validate(json.loads(path.read_text(encoding="utf-8")))

    assert set(contract.scenarios) == set(REQUIRED_SCENARIOS)
    assert contract.cleanup
    assert all(step.resource_id for step in contract.cleanup)


def test_remote_acceptance_gate_requires_deployed_image_evidence() -> None:
    manifest = json.loads((ROOT / "release/gates/release-gates.json").read_text(encoding="utf-8"))
    gate = next(item for item in manifest["gates"] if item["id"] == "remote-acceptance")

    assert "image" in gate["binds"]
    assert "deployed-suite" in gate["requires"]
    assert "deployment" in gate["kinds"]


def test_remote_report_must_match_candidate_and_complete_every_scenario(
    manifest, candidate, evidence
) -> None:
    from gates.validate import validate_gate

    gate = next(item for item in manifest["gates"] if item["id"] == "remote-acceptance")
    record = json.loads((evidence / gate["record"]).read_text(encoding="utf-8"))
    report_path = evidence / record["kinds"]["deployment"]
    report = json.loads(report_path.read_text(encoding="utf-8"))

    report["candidate_digest"] = "sha256:" + "0" * 64
    report_path.write_text(json.dumps(report), encoding="utf-8")
    result = validate_gate(gate, candidate, evidence)
    assert result.status == "mismatched"
    assert "candidate digest" in result.reason

    from gates.identity import identity_digest

    report["candidate_digest"] = identity_digest(candidate)
    report["scenarios"] = report["scenarios"][:-1]
    report_path.write_text(json.dumps(report), encoding="utf-8")
    result = validate_gate(gate, candidate, evidence)
    assert result.status == "partial"
    assert "required scenarios" in result.reason


def test_remote_report_failure_or_cleanup_failure_blocks_gate(
    manifest, candidate, evidence
) -> None:
    from gates.validate import validate_gate

    gate = next(item for item in manifest["gates"] if item["id"] == "remote-acceptance")
    record = json.loads((evidence / gate["record"]).read_text(encoding="utf-8"))
    report_path = evidence / record["kinds"]["deployment"]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["cleanup"][0]["status"] = "failed"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    result = validate_gate(gate, candidate, evidence)

    assert result.status == "failed"
    assert "cleanup" in result.reason


def test_remote_report_rejects_extra_or_malformed_scenarios_without_crashing(
    manifest, candidate, evidence
) -> None:
    from gates.validate import validate_gate

    gate = next(item for item in manifest["gates"] if item["id"] == "remote-acceptance")
    record = json.loads((evidence / gate["record"]).read_text(encoding="utf-8"))
    report_path = evidence / record["kinds"]["deployment"]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["scenarios"].append("not-a-scenario")
    report_path.write_text(json.dumps(report), encoding="utf-8")

    result = validate_gate(gate, candidate, evidence)

    assert result.status == "partial"
    assert "exactly the required scenarios" in result.reason


def test_release_candidate_runs_suite_and_records_its_report() -> None:
    workflow = _workflow(ROOT / ".github/workflows/release-zeroth-core.yml")
    job = workflow["jobs"]["smoke-from-testpypi"]
    script = _script(job)

    assert "ZEROTH_ACCEPTANCE_BASE_URL" in job["env"]
    assert "ZEROTH_ACCEPTANCE_TENANT_ID" in job["env"]
    assert "secrets.ZEROTH_ACCEPTANCE_OPERATOR_KEY" in str(job["env"])
    assert "secrets.ZEROTH_ACCEPTANCE_REVIEWER_KEY" in str(job["env"])
    assert "secrets.ZEROTH_ACCEPTANCE_ADMIN_KEY" in str(job["env"])
    assert "python -m release.acceptance.cli" in script
    assert "release/acceptance/contracts/fixture-v1.json" in script
    assert '--result "deployed-suite=${DEPLOYED_ACCEPTANCE_STATUS}"' in script
    assert "--kind deployment=release/evidence/deployed-acceptance-report.json" in script

    uploads = [
        step
        for step in job["steps"]
        if str(step.get("uses", "")).startswith("actions/upload-artifact")
    ]
    assert "deployed-acceptance-report.json" in str(uploads[-1]["with"]["path"])


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
