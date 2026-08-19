from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from release.app_certification import (
    AppDeclaration,
    CertificationReport,
    CertificationRunner,
    CheckResult,
    MANDATORY_CHECKS,
    file_digest,
    write_report,
)
from release.app_certification.workflow_finalizer import finalize_workflow
from tests.app_certification.workflow_fixtures import successful_cleanup_document


ROOT = Path(__file__).parents[2]
WORKFLOW = ROOT / ".github/workflows/app-certification.yml"


def _declaration_data() -> dict:
    return {
        "schema_version": 2,
        "app_name": "reference-app",
        "zeroth_version": "0.23.9.9",
        "lock_path": "uv.lock",
        "dockerfile": "Dockerfile.certification",
        "image_reference": "reference-app:certification",
        "sbom_path": "evidence/app.spdx.json",
        "provenance_path": "evidence/provenance.json",
        "targets": {
            "graph_builders": ["apps.vendor_dd.graphs:build_main_graph"],
            "contracts": "apps.vendor_dd.contracts:CONTRACTS",
            "auth_config": "apps.vendor_dd.entrypoint:build_auth_config",
            "policy_guard": "apps.vendor_dd.entrypoint:build_policy_guard",
            "migration_runner": "apps.vendor_dd.migrations:migrate",
            "frontend_path": "frontend",
        },
        "smoke": {
            "method": "POST",
            "path": "/v1/runs",
            "request_json": {"input_payload": {"case": "fixed"}},
            "expected_status": 202,
            "expected_json": {"status": "accepted", "result": {"case": "fixed"}},
        },
    }


def _valid_contract_schema() -> dict:
    return {
        "properties": {"value": {"title": "Value", "type": "string"}},
        "required": ["value"],
        "title": "Payload",
        "type": "object",
    }


def test_reflective_candidate_code_has_no_authoritative_result_channel(tmp_path: Path) -> None:
    marker = tmp_path / "candidate-ran"
    source = tmp_path / "candidate_attack.py"
    source.write_text(
        "import hashlib, json, os\n"
        f"open({str(marker)!r}, 'w').write('executed')\n"
        "source_digest = 'sha256:' + hashlib.sha256(open(__file__, 'rb').read()).hexdigest()\n"
        f"payload = {{'check': 'contracts', 'evidence': {{'contracts': {{'Payload': {_valid_contract_schema()!r}}}}}, "
        "'schema_version': 1, 'target_sources': {'candidate_attack:CONTRACTS': source_digest}}\n"
        "emit = getattr(os, 'write')\n"
        "finish = getattr(os, '_exit')\n"
        "emit(1, (json.dumps(payload, sort_keys=True, separators=(',', ':')) + '\\n').encode())\n"
        "finish(0)\n"
        "CONTRACTS = {}\n",
        encoding="utf-8",
    )
    (tmp_path / "certification.semantic.json").write_text(
        json.dumps(
            {
                "capabilities": {},
                "schema_version": 1,
                "contracts": {"Payload": _valid_contract_schema()},
                "graphs": [],
                "policies": {},
                "reducers": [],
                "service_config": {"auth_config": {}, "database_backend": "sqlite"},
                "target_sources": {"candidate_attack:CONTRACTS": file_digest(source)},
                "zeroth_version": "0.23.9.9",
            }
        ),
        encoding="utf-8",
    )
    data = _declaration_data()
    data["targets"]["contracts"] = "candidate_attack:CONTRACTS"
    (tmp_path / "certification.json").write_text(json.dumps(data), encoding="utf-8")

    result = CertificationRunner(
        tmp_path, AppDeclaration.model_validate(data), check_python=Path(sys.executable)
    )._command("contracts")

    assert result.status == "passed", result.detail
    assert not marker.exists(), "certification executed candidate Python for a trusted verdict"


def test_candidate_user_inventory_never_kills_detached_descendants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from release.app_certification import candidate_supervisor

    calls: list[list[str]] = []

    def run(argv: list[str], **_kwargs) -> subprocess.CompletedProcess:
        calls.append(argv)
        assert argv[0] == "pgrep"
        return subprocess.CompletedProcess(argv, 0, stdout="123\n456\n")

    monkeypatch.setattr(candidate_supervisor.subprocess, "run", run)

    with pytest.raises(RuntimeError, match="leak|survived"):
        candidate_supervisor._terminate_candidate_user("app-cert-candidate")

    assert calls == [["pgrep", "-u", "app-cert-candidate"]]


def test_candidate_build_and_dependency_logs_have_hard_resource_boundaries() -> None:
    from release.app_certification.dependency_sandbox import _container_argv

    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["certify"]
    image = next(step["run"] for step in job["steps"] if step.get("id") == "image")

    assert "run-build-sandbox" in image
    for option in (
        "--builder-name",
        "--cpus",
        "--memory",
        "--pids-limit",
        "--disk-bytes",
        "--timeout",
        "--output-limit",
    ):
        assert option in image
    dependency = _container_argv(
        SimpleNamespace(
            app_root=ROOT,
            container_name="dependency-sandbox",
            cpus=2,
            docker="docker",
            gid=10001,
            image="python:3.12",
            memory="2g",
            pids_limit=128,
            uid=10001,
            uv=ROOT / "uv.lock",
        ),
        Path("/venv"),
    )
    assert "--log-driver" in dependency
    assert "max-size=1m" in dependency
    assert "max-file=1" in dependency


def test_enabled_regulus_dependencies_are_in_the_certified_runtime_lock() -> None:
    locked = (ROOT / "requirements-image.txt").read_text(encoding="utf-8")

    for package in ("dramatiq", "email-validator", "python-jose"):
        assert f"{package}==" in locked


def _failed_report() -> CertificationReport:
    return CertificationReport(
        status="failed",
        candidate=None,
        checks=[
            CheckResult(name=name, status="failed", detail=f"{name}: fixture failure")
            for name in MANDATORY_CHECKS
        ],
        evidence=None,
    )


def test_cleanup_is_a_retained_hashed_workflow_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cleanup = tmp_path / "cleanup.json"
    cleanup.write_text(
        json.dumps(successful_cleanup_document()) + "\n",
        encoding="utf-8",
    )
    report = tmp_path / "report.json"
    write_report(_failed_report(), report)
    for name in (
        "APP_CHECKOUT",
        "CERTIFIER_CHECKOUT",
        "PREPARE",
        "IMAGE",
        "WHEEL",
        "SBOM",
        "EVIDENCE",
        "CONTAINERS",
        "HEALTH",
        "RUNTIME",
        "CERTIFY",
        "CLEANUP",
    ):
        monkeypatch.setenv(name, "success")

    assert finalize_workflow(tmp_path) == 0

    stages = json.loads((tmp_path / "workflow-stages.json").read_text(encoding="utf-8"))
    retained = json.loads((tmp_path / "workflow-evidence.json").read_text(encoding="utf-8"))
    assert stages["cleanup"] == "success"
    assert retained == {
        "schema_version": 1,
        "cleanup_sha256": file_digest(cleanup),
        "report_sha256": file_digest(report),
        "workflow_stages_sha256": file_digest(tmp_path / "workflow-stages.json"),
    }


def test_cleanup_precedes_finalization_and_is_verified_from_retained_hashes() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    certify = workflow["jobs"]["certify"]["steps"]
    names = [step["name"] for step in certify]
    finalizer_index = names.index("Finalize canonical report and stage diagnostics")
    cleanup_indices = [index for index, name in enumerate(names) if name.startswith("Clean up ")]
    finalizer = certify[finalizer_index]
    verify = next(
        step["run"] for step in workflow["jobs"]["verify"]["steps"] if step.get("id") == "validate"
    )

    assert cleanup_indices and max(cleanup_indices) < finalizer_index
    assert finalizer["env"]["CLEANUP"] == "${{ steps.cleanup.outcome }}"
    assert "--workflow-evidence evidence/workflow-evidence.json" in verify
    assert "--cleanup evidence/cleanup.json" in verify
    assert "--workflow-stages evidence/workflow-stages.json" in verify
