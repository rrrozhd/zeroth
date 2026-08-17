from __future__ import annotations

import base64
import hashlib
import json
import sys
import zipfile
from pathlib import Path

import pytest

from release.app_certification import (
    MANDATORY_CHECKS,
    AppDeclaration,
    CertificationReport,
    CertificationRunner,
    CheckResult,
    CommandResult,
    file_digest,
    write_report,
)
from release.app_certification.cli import main as certification_main
from release.app_certification.wheel_installation import (
    RUNTIME_BOOTSTRAP,
    TRUSTED_RUNTIME_IMAGE,
)
from release.app_certification.workflow_finalizer import finalize_workflow
from tests.app_certification.test_engine import declaration_data


WORKFLOW = Path(".github/workflows/app-certification.yml")


def _capture_failure(calls: list[list[str]]):
    def execute(argv: list[str], cwd: Path) -> CommandResult:
        del cwd
        calls.append(argv)
        return CommandResult(17, "", "capture sentinel")

    return execute


def test_trusted_supervisors_exclude_candidate_site_packages(tmp_path: Path) -> None:
    trusted, candidate = [], []
    app_python = tmp_path / ".venv/bin/python"
    runner = CertificationRunner(
        tmp_path,
        AppDeclaration.model_validate(declaration_data()),
        executor=_capture_failure(trusted),
        candidate_executor=_capture_failure(candidate),
        check_python=app_python,
    )

    runner._command("dependency-lock")
    runner._command("contracts")

    assert len(trusted) == len(candidate) == 1
    certifier_venv = str(Path(sys.executable).parent.parent.resolve())
    assert trusted[0][6] == candidate[0][6] == certifier_venv
    assert str(app_python.parent.parent) not in trusted[0]
    assert candidate[0].count(str(app_python.parent.parent)) == 1


def test_candidate_supervisor_receives_app_venv_only_as_untrusted_input(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    app_venv = tmp_path / ".venv"
    runner = CertificationRunner(
        tmp_path,
        AppDeclaration.model_validate(declaration_data()),
        candidate_executor=_capture_failure(calls),
        check_python=app_venv / "bin/python",
    )

    runner._command("contracts")

    argv = calls[0]
    assert argv[argv.index("--candidate-venv") + 1] == str(app_venv)


def test_production_service_route_rejects_shape_compatible_auth_config(tmp_path: Path) -> None:
    (tmp_path / "fake_auth.py").write_text(
        "class FakeAuth:\n"
        "    def model_dump(self, mode='json'):\n"
        "        return {'api_keys': [{'credential_id': 'fake', 'secret': 'fake', "
        "'subject': 'fake', 'roles': ['admin'], 'tenant_id': 'tenant', "
        "'workspace_id': None}], 'bearer': None, 'custom_roles': {}, "
        "'revoked_credential_ids': []}\n"
        "def build_auth_config():\n"
        "    return FakeAuth()\n",
        encoding="utf-8",
    )
    data = declaration_data()
    data["targets"]["auth_config"] = "fake_auth:build_auth_config"
    result = CertificationRunner(
        tmp_path,
        AppDeclaration.model_validate(data),
        check_python=Path(sys.executable),
    )._command("service-config")

    assert result.status == "failed"
    assert "ServiceAuthConfig" in result.detail


def test_workflow_finalizer_preserves_valid_failed_certifier_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "handoff"
    detail = "service-config: auth_config target must return ServiceAuthConfig"
    report = CertificationReport(
        status="failed",
        candidate=None,
        evidence=None,
        checks=[
            CheckResult(
                name=name,
                status="failed",
                detail=detail if name == "service-config" else f"{name}: retained failure",
            )
            for name in MANDATORY_CHECKS
        ],
    )
    write_report(report, root / "report.json")
    for stage in (
        "APP_CHECKOUT",
        "CERTIFIER_CHECKOUT",
        "PREPARE",
        "IMAGE",
        "SBOM",
        "EVIDENCE",
        "CONTAINERS",
        "HEALTH",
        "CERTIFY",
    ):
        monkeypatch.setenv(stage, "failure" if stage == "CERTIFY" else "success")

    assert finalize_workflow(root) == 0

    retained = json.loads((root / "report.json").read_text(encoding="utf-8"))
    service = next(item for item in retained["checks"] if item["name"] == "service-config")
    assert service["detail"] == detail


def _record_digest(payload: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=").decode()
    return f"sha256={encoded}"


def _wheel_fixture(tmp_path: Path) -> tuple[Path, Path]:
    wheel = tmp_path / "zeroth_core-0.23.9.9-py3-none-any.whl"
    site_packages = tmp_path / "site-packages"
    members = {
        "zeroth/__init__.py": b'__version__ = "0.23.9.9"\n',
        "zeroth_core-0.23.9.9.dist-info/METADATA": (
            b"Metadata-Version: 2.1\nName: zeroth-core\nVersion: 0.23.9.9\n"
        ),
    }
    record_name = "zeroth_core-0.23.9.9.dist-info/RECORD"
    record = (
        "".join(
            f"{name},{_record_digest(payload)},{len(payload)}\n"
            for name, payload in members.items()
        )
        + f"{record_name},,\n"
    )
    members[record_name] = record.encode()
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
            installed = site_packages / name
            installed.parent.mkdir(parents=True, exist_ok=True)
            installed.write_bytes(payload)
    return wheel, site_packages


def test_exact_wheel_contents_are_verified_outside_the_candidate(tmp_path: Path) -> None:
    wheel, site_packages = _wheel_fixture(tmp_path)
    manifest = tmp_path / "installed-wheel.json"
    image_config = tmp_path / "image-config.json"
    image_config.write_text(
        json.dumps(
            {
                "Cmd": [
                    "/usr/local/bin/python",
                    "-I",
                    "-S",
                    "-c",
                    RUNTIME_BOOTSTRAP,
                    "run-certified-runtime",
                    "/usr/local/lib/python3.12/site-packages",
                    "/opt/app",
                    "candidate.entrypoint",
                ],
                "Entrypoint": None,
                "Labels": {"dev.zeroth.certification.runtime-base": TRUSTED_RUNTIME_IMAGE},
            }
        ),
        encoding="utf-8",
    )
    argv = [
        "verify-wheel-installation",
        "--wheel",
        str(wheel),
        "--site-packages",
        str(site_packages),
        "--image-config",
        str(image_config),
        "--image-digest",
        "sha256:" + "a" * 64,
        "--output",
        str(manifest),
    ]

    assert certification_main(argv) == 0
    proof = json.loads(manifest.read_text(encoding="utf-8"))
    assert proof["wheel_sha256"] == file_digest(wheel)
    assert proof["package"] == "zeroth-core"

    (site_packages / "zeroth/__init__.py").write_text("tampered\n", encoding="utf-8")
    assert certification_main(argv) == 2


def test_workflow_extracts_and_binds_installed_wheel_without_execution() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "docker create --name app-cert-wheel-inspect" in workflow
    assert "docker cp app-cert-wheel-inspect:/usr/local/lib/python3.12/site-packages/." in workflow
    assert "verify-wheel-installation" in workflow
    assert '--wheel-installation "$HANDOFF_ROOT/installed-wheel.json"' in workflow
    assert "--wheel-installation evidence/materials/installed-wheel.json" in workflow
    assert "docker rm app-cert-wheel-inspect" in workflow
