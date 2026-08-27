from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import uuid
import zipfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
import yaml

from release.app_certification import (
    MANDATORY_CHECKS,
    AppDeclaration,
    CertificationReport,
    CertificationRunner,
    CheckResult,
    file_digest,
    scaffold_checkout,
    write_report,
)
from release.app_certification.scaffold import generate_semantic_manifest
from release.app_certification.workflow_finalizer import finalize_workflow
from tests.app_certification.workflow_fixtures import write_generated_app
from tests.conftest import requires_docker


ROOT = Path(__file__).parents[2]
WORKFLOW = ROOT / ".github/workflows/app-certification.yml"


def _scaffold(root: Path, module: str = "generated_app") -> AppDeclaration:
    write_generated_app(root, module)
    (root / "frontend").mkdir()
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    scaffold_checkout(
        root,
        app_name="generated",
        module=module,
        zeroth_version="0.23.9.16",
        zeroth_ref="a" * 40,
    )
    return AppDeclaration.model_validate_json(
        (root / "certification.json").read_text(encoding="utf-8")
    )


def test_declared_postgres_cannot_fall_back_to_ambient_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    declaration = _scaffold(tmp_path, "backend_app")
    generate_semantic_manifest(
        tmp_path,
        declaration,
        tmp_path / declaration.semantic_path,
        database_backend="postgres",
    )
    monkeypatch.delenv("ZEROTH_DATABASE__BACKEND", raising=False)
    monkeypatch.delenv("ZEROTH_DATABASE__POSTGRES_DSN", raising=False)

    result = CertificationRunner(
        tmp_path,
        declaration,
        check_python=Path(sys.executable),
    )._command("migrations")

    assert result.status == "failed"
    assert "declared database backend postgres does not match runtime backend sqlite" in result.detail


def test_callable_workflow_binds_or_rejects_the_declared_database_backend() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["certify"]["steps"]
    prepare = next(step["run"] for step in steps if step.get("id") == "prepare")
    containers = next(step["run"] for step in steps if step.get("id") == "containers")

    assert "callable workflow does not provision a fresh PostgreSQL database" in prepare
    assert 'echo "database_backend=$DATABASE_BACKEND" >> "$GITHUB_OUTPUT"' in prepare
    assert 'echo "ZEROTH_DATABASE__BACKEND=$DATABASE_BACKEND" >> "$GITHUB_ENV"' in prepare
    assert containers.count("--env ZEROTH_DATABASE__BACKEND") == 2


def _wait_ready(process: subprocess.Popen[str], url: str) -> bool:
    deadline = time.monotonic() + 10
    while process.poll() is None and time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=0.2) as response:
                if json.load(response) == {"status": "ok"}:
                    return True
        except OSError:
            time.sleep(0.05)
    return False


def _smoke_request(
    declaration: AppDeclaration, base_url: str, *, api_key: str
) -> tuple[int, object]:
    request = Request(
        f"{base_url}{declaration.smoke.path}",
        data=json.dumps(declaration.smoke.request_json).encode(),
        method=declaration.smoke.method,
        headers={"Content-Type": "application/json", "X-API-Key": api_key},
    )
    with urlopen(request, timeout=2) as response:
        return response.status, json.load(response)


def test_scaffolded_runtime_executes_health_and_authenticated_smoke(tmp_path: Path) -> None:
    declaration = _scaffold(tmp_path)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    environment = {
        **os.environ,
        "APP_CERTIFICATION_API_KEY": "runtime-secret",
        "HOST": "127.0.0.1",
        "PORT": str(port),
        "PYTHONPATH": str(tmp_path),
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "generated_app.certification_entrypoint"],
        cwd=tmp_path,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready = _wait_ready(process, f"http://127.0.0.1:{port}/health/ready")
        health = subprocess.run(
            [sys.executable, "-m", "generated_app.certification_healthcheck"],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            timeout=5,
        )
        with pytest.raises(HTTPError) as rejected:
            _smoke_request(declaration, f"http://127.0.0.1:{port}", api_key="wrong")
        status, payload = _smoke_request(
            declaration, f"http://127.0.0.1:{port}", api_key="runtime-secret"
        )
    finally:
        if process.poll() is None:
            process.terminate()
        stdout, stderr = process.communicate(timeout=5)

    assert ready, f"generated runtime did not become ready: {stdout[-500:]} {stderr[-500:]}"
    assert health.returncode == 0, health.stderr
    assert rejected.value.code == 401
    assert status == declaration.smoke.expected_status
    assert payload == declaration.smoke.expected_json
    dockerfile = (tmp_path / declaration.dockerfile).read_text(encoding="utf-8")
    assert "generated_app.certification_entrypoint" in dockerfile


def _minimal_zeroth_wheel(path: Path) -> None:
    distribution = "zeroth_core-0.23.9.16.dist-info"
    members = {
        "zeroth/__init__.py": '__version__ = "0.23.9.16"\n',
        f"{distribution}/METADATA": (
            "Metadata-Version: 2.1\nName: zeroth-core\nVersion: 0.23.9.16\n"
        ),
        f"{distribution}/WHEEL": (
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    record = f"{distribution}/RECORD"
    members[record] = "".join(f"{name},,\n" for name in (*members, record))
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)


def _build_image(root: Path, dockerfile: Path, image: str) -> None:
    result = subprocess.run(
        ["docker", "build", "--pull=false", "--file", str(dockerfile), "--tag", image, str(root)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr[-2000:]


def _start_container(image: str, container: str) -> str:
    result = subprocess.run(
        [
            "docker",
            "run",
            "--detach",
            "--name",
            container,
            "--publish",
            "127.0.0.1::8000",
            "--env",
            "APP_CERTIFICATION_API_KEY=image-secret",
            image,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    port = subprocess.run(
        ["docker", "port", container, "8000/tcp"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert port.returncode == 0, port.stderr
    return port.stdout.strip().rsplit(":", 1)[1]


def _wait_for_image(url: str) -> bool:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=0.5) as response:
                if json.load(response) == {"status": "ok"}:
                    return True
        except OSError:
            time.sleep(0.1)
    return False


def _remove_image_container(image: str, container: str) -> None:
    subprocess.run(
        ["docker", "rm", "--force", container],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=20,
    )
    subprocess.run(
        ["docker", "image", "rm", image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )


@requires_docker
def test_scaffolded_image_reaches_health_and_authenticated_smoke(tmp_path: Path) -> None:
    declaration = _scaffold(tmp_path, "image_app")
    certifier = tmp_path / ".zeroth-certifier"
    certifier.mkdir()
    (certifier / "requirements-image.txt").write_text("", encoding="utf-8")
    _minimal_zeroth_wheel(certifier / "zeroth_core-0.23.9.16-py3-none-any.whl")
    suffix = uuid.uuid4().hex
    image = f"app-cert-scaffold-audit18:{suffix}"
    container = f"app-cert-scaffold-audit18-{suffix}"
    _build_image(tmp_path, tmp_path / declaration.dockerfile, image)
    try:
        port = _start_container(image, container)
        base_url = f"http://127.0.0.1:{port}"
        assert _wait_for_image(f"{base_url}/health/ready")
        status, payload = _smoke_request(declaration, base_url, api_key="image-secret")
        assert status == declaration.smoke.expected_status
        assert payload == declaration.smoke.expected_json
    finally:
        _remove_image_container(image, container)


def _failed_report(detail: str) -> CertificationReport:
    return CertificationReport(
        status="failed",
        candidate=None,
        evidence=None,
        checks=[
            CheckResult(
                name=name,
                status="failed",
                detail=detail if name == "migrations" else f"{name}: retained failure",
            )
            for name in MANDATORY_CHECKS
        ],
    )


def _write_cleanup_failure(root: Path) -> None:
    (root / "cleanup.json").write_text(
        json.dumps(
            {
                "daemon_id": "daemon",
                "errors": ["network removal failed"],
                "resources": [],
                "run_id": "audit18-cleanup",
                "schema_version": 1,
                "status": "failed",
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_cleanup_failure_preserves_certifier_diagnostics_and_is_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "handoff"
    detail = "migrations: broken PostgreSQL migration sentinel"
    write_report(_failed_report(detail), root / "report.json")
    _write_cleanup_failure(root)
    for stage in (
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
        monkeypatch.setenv(stage, "failure" if stage in {"CERTIFY", "CLEANUP"} else "success")

    assert finalize_workflow(root) == 0

    retained = json.loads((root / "report.json").read_text(encoding="utf-8"))
    migration = next(item for item in retained["checks"] if item["name"] == "migrations")
    stages = json.loads((root / "workflow-stages.json").read_text(encoding="utf-8"))
    evidence = json.loads((root / "workflow-evidence.json").read_text(encoding="utf-8"))
    assert migration["detail"] == detail
    assert stages["cleanup"] == "failure"
    assert evidence["cleanup_sha256"] == file_digest(root / "cleanup.json")
    assert evidence["report_sha256"] == file_digest(root / "report.json")
    assert evidence["workflow_stages_sha256"] == file_digest(root / "workflow-stages.json")
