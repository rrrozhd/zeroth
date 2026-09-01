from __future__ import annotations

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def _workflow(path: Path) -> dict:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    value["on"] = value.pop(True, value.get("on"))
    return value


def _script(job: dict) -> str:
    return "\n".join(step.get("run", "") for step in job["steps"] if isinstance(step, dict))


def test_remote_acceptance_is_bound_to_the_headless_economic_product() -> None:
    manifest = json.loads((ROOT / "release/gates/release-gates.json").read_text())
    gate = next(item for item in manifest["gates"] if item["id"] == "remote-acceptance")

    assert gate["binds"] == ["commit", "package"]
    assert gate["requires"] == [
        "index-install",
        "headless-install",
        "diagnostic-artifact",
        "provider-reconciliation",
    ]
    assert gate["kinds"] == ["economic-acceptance"]
    assert "deployed" not in gate["description"].lower()


def test_release_build_and_testpypi_acceptance_exclude_ui_sdk_and_provider_calls() -> None:
    workflow = _workflow(ROOT / ".github/workflows/release-zeroth-core.yml")
    build = workflow["jobs"]["build"]
    smoke = workflow["jobs"]["smoke-install"]
    acceptance = workflow["jobs"]["smoke-from-testpypi"]
    build_script = _script(build)
    smoke_script = _script(smoke)
    acceptance_script = _script(acceptance)

    assert acceptance["name"] == "TestPyPI install + economic debugger acceptance"
    assert "frontend" not in build_script
    assert "build_console_dist" not in build_script
    assert "zeroth-console" not in smoke_script
    assert "zeroth-console==" not in acceptance_script
    assert "zeroth-sdk" not in build_script
    assert "OPENAI_API_KEY" not in str(acceptance)
    assert "ZEROTH_ACCEPTANCE_" not in str(acceptance)
    assert "examples/00_hello.py" not in acceptance_script
    assert "release.acceptance.cli" not in acceptance_script
    assert "zeroth-econ diagnose" in acceptance_script
    assert "zeroth-econ reconcile" in acceptance_script
    assert "economic-acceptance-report.json" in acceptance_script
    assert "--kind economic-acceptance=" in acceptance_script


def test_installed_acceptance_uses_the_authenticated_instrumentation_contract() -> None:
    acceptance = (ROOT / "release" / "economic_acceptance.py").read_text(
        encoding="utf-8"
    )

    assert "InstrumentationClient.authenticated" in acceptance
    assert "track_execution_confirmed" in acceptance
    assert "track_outcome_confirmed" in acceptance


def test_legacy_platform_acceptance_remains_manual_and_non_promoting() -> None:
    release = _workflow(ROOT / ".github/workflows/release-zeroth-core.yml")
    legacy = _workflow(ROOT / ".github/workflows/deployed-acceptance.yml")

    assert set(legacy["on"]) == {"workflow_dispatch"}
    assert "deployed-acceptance" in legacy["jobs"]
    assert "deployed-acceptance" not in release["jobs"]
    assert "deployed-acceptance.yml" not in str(release)


def test_candidate_release_stops_at_testpypi_and_exports_one_promotion_bundle() -> None:
    workflow = _workflow(ROOT / ".github/workflows/release-zeroth-core.yml")

    assert "publish-pypi" not in workflow["jobs"]
    assert "evidence-gate-final" not in workflow["jobs"]
    assembly = workflow["jobs"]["assemble-promotion-candidate"]
    script = _script(assembly)
    assert set(assembly["needs"]) == {"evidence-gate", "smoke-from-testpypi"}
    assert "--gate remote-acceptance" in script
    uploads = [
        step
        for step in assembly["steps"]
        if str(step.get("uses", "")).startswith("actions/upload-artifact")
    ]
    assert uploads[-1]["with"]["name"] == "promotion-candidate"
    assert "dist" in str(uploads[-1]["with"]["path"])
    assert "release/evidence" in str(uploads[-1]["with"]["path"])


def test_manual_promotion_is_bound_to_candidate_run_digest_and_human_intent() -> None:
    workflow = _workflow(ROOT / ".github/workflows/promote-zeroth-core.yml")
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    job = workflow["jobs"]["promote"]
    script = _script(job)

    assert {"candidate_run_id", "candidate_digest", "confirmation"} <= set(inputs)
    assert all(inputs[name]["required"] for name in inputs)
    assert job["environment"] == "pypi"
    assert "PROMOTE_ZEROTH_CORE" in script
    assert "gh run download" in script
    assert "promotion-candidate" in script
    assert "actions/checkout" in str(job["steps"])
    assert "--gate promotion" in script
    assert "--phase final" in script
    assert "pypa/gh-action-pypi-publish" in str(job["steps"])
    assert "release/signoff/" not in script


def test_economic_acceptance_report_is_candidate_bound_and_fail_closed(
    manifest, candidate, evidence
) -> None:
    from gates.validate import validate_gate

    gate = next(item for item in manifest["gates"] if item["id"] == "remote-acceptance")
    record = json.loads((evidence / gate["record"]).read_text(encoding="utf-8"))
    report_path = evidence / record["kinds"]["economic-acceptance"]
    report = json.loads(report_path.read_text(encoding="utf-8"))

    report["candidate_digest"] = "sha256:" + "0" * 64
    report_path.write_text(json.dumps(report), encoding="utf-8")
    result = validate_gate(gate, candidate, evidence)
    assert result.status == "mismatched"


def test_economic_acceptance_rejects_ui_or_sdk_and_unclosed_bills(
    manifest, candidate, evidence
) -> None:
    from gates.validate import validate_gate

    gate = next(item for item in manifest["gates"] if item["id"] == "remote-acceptance")
    record = json.loads((evidence / gate["record"]).read_text(encoding="utf-8"))
    report_path = evidence / record["kinds"]["economic-acceptance"]
    original = json.loads(report_path.read_text(encoding="utf-8"))

    with_ui = json.loads(json.dumps(original))
    with_ui["excluded_distributions"]["zeroth-console"] = "present"
    report_path.write_text(json.dumps(with_ui), encoding="utf-8")
    result = validate_gate(gate, candidate, evidence)
    assert result.status == "failed"
    assert "headless" in result.reason

    unclosed = json.loads(json.dumps(original))
    unclosed["reconciliation"]["reconciliation_state"] = "unreconciled"
    report_path.write_text(json.dumps(unclosed), encoding="utf-8")
    result = validate_gate(gate, candidate, evidence)
    assert result.status == "failed"
    assert "reconciliation" in result.reason


def test_economic_acceptance_compares_decimal_zero_by_value(
    manifest, candidate, evidence
) -> None:
    from gates.validate import validate_gate

    gate = next(item for item in manifest["gates"] if item["id"] == "remote-acceptance")
    record = json.loads((evidence / gate["record"]).read_text(encoding="utf-8"))
    report_path = evidence / record["kinds"]["economic-acceptance"]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["reconciliation"]["unreconciled_billed_usd"] = "0E-8"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    result = validate_gate(gate, candidate, evidence)

    assert result.status == "passed"
