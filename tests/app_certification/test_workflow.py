from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

import pytest
import yaml

from release.app_certification import (
    AppDeclaration,
    CertificationRunner,
    CommandResult,
    load_declaration,
)
from tests.app_certification.test_engine import declaration_data


ROOT = Path(__file__).parents[2]
REUSABLE = ROOT / ".github/workflows/app-certification.yml"
CALLER = ROOT / ".github/workflows/vendor-dd-certification.yml"
DECLARATION = ROOT / "apps/vendor_dd/certification.json"
DOCKERFILE = ROOT / "apps/vendor_dd/Dockerfile.certification"
CHECKOUT = "actions/checkout@11d5960a326750d5838078e36cf38b85af677262"
ACTION_PINS = {
    "actions/checkout": "11d5960a326750d5838078e36cf38b85af677262",
    "astral-sh/setup-uv": "11f9893b081a58869d3b5fccaea48c9e9e46f990",
    "anchore/sbom-action": "e22c389904149dbc22b58101806040fa8d37a610",
    "actions/attest": "1e69f48acb82d1966a394da916b4c1698aa569d6",
    "actions/upload-artifact": "ea165f8d65b6e75b540449e92b4886f43607fa02",
    "actions/download-artifact": "d3f86a106a0bac45b974a628896c90dbdf5c8093",
}
PRIVILEGED = {
    "contents": "read",
    "attestations": "write",
    "id-token": "write",
    "artifact-metadata": "write",
}


def _load(path: Path = REUSABLE) -> dict:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["on"] = document.pop(True, document.get("on"))
    return document


def _steps(job: str = "certify") -> list[dict]:
    return _load()["jobs"][job]["steps"]


def _step(step_id: str, job: str = "certify") -> dict:
    return next(step for step in _steps(job) if step.get("id") == step_id)


def test_reusable_workflow_has_candidate_verifier_and_privileged_finalizer() -> None:
    workflow = _load()
    candidate = workflow["jobs"]["certify"]
    verifier = workflow["jobs"]["verify"]
    attestation = workflow["jobs"]["attest"]

    assert set(workflow["on"]) == {"workflow_call", "workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    assert candidate["permissions"] == {"contents": "read"}
    assert verifier["permissions"] == {"contents": "read"}
    assert verifier["needs"] == "certify"
    assert attestation["permissions"] == PRIVILEGED
    assert attestation["needs"] == "verify"
    assert attestation["if"] == "${{ needs.verify.result == 'success' }}"


def test_caller_grants_privilege_only_to_the_reusable_job_contract() -> None:
    caller = _load(CALLER)
    job = caller["jobs"]["certify-vendor-dd"]

    assert caller["permissions"] == {"contents": "read"}
    assert job["permissions"] == PRIVILEGED
    assert job["uses"] == "./.github/workflows/app-certification.yml"
    assert "steps" not in job and "runs-on" not in job
    assert job["with"]["zeroth_ref"] == "${{ github.sha }}"
    assert job["with"]["declaration_path"] == "apps/vendor_dd/certification.json"


def test_candidate_job_uses_the_app_environment_and_no_privileged_action() -> None:
    steps = _steps()
    scripts = "\n".join(step.get("run", "") for step in steps)
    actions = [step["uses"] for step in steps if "uses" in step]
    prepare = _step("prepare")["run"]

    assert "uv sync --directory app --frozen --all-extras" in prepare
    assert "app/.venv/bin/python" in prepare
    assert "uv build --directory zeroth --wheel" in prepare
    assert "npm ci --prefix /home/app-cert-candidate/frontend" in prepare
    assert "--check-python app/.venv/bin/python" in _step("certify")["run"]
    assert "--untrusted-user app-cert-candidate" in _step("certify")["run"]
    assert not any(action.startswith("actions/attest@") for action in actions)
    assert "docker push" not in scripts and "kubectl" not in scripts


def test_candidate_execution_cannot_write_certifier_or_handoff() -> None:
    prepare = _step("prepare")["run"]
    certify = _step("certify")["run"]

    assert "useradd" in prepare and "app-cert-candidate" in prepare
    assert prepare.index("uv sync --directory zeroth") < prepare.index("app-cert-candidate")
    assert "sudo -H -u app-cert-candidate" in prepare
    assert 'sudo chown -R "$USER":"$USER" app/.venv' in prepare
    assert "chmod -R go-w zeroth app" in prepare
    assert "mkdir -m 700 app/.app-certification" in prepare
    assert "zeroth/.venv/bin/python -m release.app_certification run" in certify


def test_container_checks_are_trusted_while_candidate_imports_stay_unprivileged(
    tmp_path: Path,
) -> None:
    trusted_calls: list[str] = []
    candidate_calls: list[str] = []

    def trusted(argv: list[str], cwd: Path) -> CommandResult:
        del cwd
        name = argv[argv.index("--root") - 1]
        trusted_calls.append(name)
        payload = {"check": name, "schema_version": 1, "status": "passed"}
        return CommandResult(0, json.dumps(payload) + "\n", "")

    def candidate(argv: list[str], cwd: Path) -> CommandResult:
        del cwd
        candidate_calls.append(argv[argv.index("--root") - 1])
        return CommandResult(1, "", "candidate has no Docker socket access")

    runner = CertificationRunner(
        tmp_path,
        AppDeclaration.model_validate(declaration_data()),
        executor=trusted,
        candidate_executor=candidate,
    )

    assert runner._command("container-startup").status == "passed"
    assert runner._command("health").status == "passed"
    assert runner._command("contracts").status == "failed"
    assert trusted_calls == ["container-startup", "health"]
    assert candidate_calls == ["contracts"]
    prepare = _step("prepare")["run"]
    useradd = next(line for line in prepare.splitlines() if "useradd" in line)
    assert "docker" not in useradd
    assert "usermod" not in prepare and "gpasswd" not in prepare


def test_frontend_checker_uses_locked_isolated_npm_tool_tree() -> None:
    prepare = _step("prepare")["run"]
    install = "npm ci --prefix /home/app-cert-candidate/frontend"
    copy = (
        "sudo cp -R /home/app-cert-candidate/frontend/node_modules "
        '"app/$FRONTEND_PATH/node_modules"'
    )

    assert "load_declaration" in prepare and ".targets.frontend_path" in prepare
    assert "app/frontend" not in prepare
    assert prepare.index(install) < prepare.index(copy)
    assert 'chown -R "$USER":"$USER" app/.venv "app/$FRONTEND_PATH/node_modules"' in prepare
    assert prepare.index(copy) < prepare.rindex("chmod -R go-w zeroth app")


def test_candidate_job_builds_two_measured_ready_boundaries() -> None:
    image = _step("image")["run"]
    containers = _step("containers")["run"]
    health = _step("health")["run"]

    assert image.count("docker build") == 1
    assert "docker image inspect" in image and "docker save" in image
    assert containers.count("docker run --detach") == 2
    assert "app-cert-packaged-net" in containers and "app-cert-ephemeral-net" in containers
    assert "--tmpfs /data:rw,noexec,nosuid,size=256m,uid=10001,gid=10001" in containers
    assert containers.count("--env APP_CERTIFICATION_API_KEY") == 2
    assert health.count("probe-readiness") == 2
    assert health.count("wait_healthy") >= 3


def test_build_context_uses_exact_committed_source_archive() -> None:
    prepare = _step("prepare")["run"]
    image = _step("image")["run"]
    evidence = _step("evidence")["run"]
    certify = _step("certify")["run"]
    validate = _step("validate", "verify")["run"]

    assert "git -C app archive" in prepare
    assert "app/.app-certification/source.tar" in prepare
    assert '--tag "$IMAGE_REFERENCE" build-context' in image
    assert '--tag "$IMAGE_REFERENCE" app' not in image
    assert '--source-digest "$SOURCE_DIGEST"' in evidence
    assert '--source-digest "$SOURCE_DIGEST"' in certify
    assert "--source-archive evidence/source.tar" in validate


def test_candidate_startup_keeps_app_off_trusted_pythonpath() -> None:
    pythonpath = _step("certify")["env"]["PYTHONPATH"]

    assert pythonpath == "${{ github.workspace }}/zeroth"


def test_every_pre_certification_failure_gets_a_canonical_report(tmp_path: Path) -> None:
    finalizer = next(
        step for step in _steps() if step["name"].startswith("Finalize canonical report")
    )
    script = finalizer["run"]

    assert finalizer["if"] == "${{ always() }}"
    assert "finalize-workflow" in script
    assert "workflow finalizer fallback" in script
    assert "workflow stage " in script and " outcome=" in script
    assert "--root app/.app-certification" in script
    for name in ("prepare", "image", "containers", "health", "certify"):
        assert name.upper() in finalizer["env"]
    fallback = script.split("|| ", 1)[1]
    result = subprocess.run(
        shlex.split(fallback),
        cwd=tmp_path,
        env={**os.environ, "APP_CHECKOUT": "success", "CERTIFIER_CHECKOUT": "failure"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads((tmp_path / "app/.app-certification/report.json").read_text())
    assert all(
        "certifier_checkout outcome=failure" in check["detail"] for check in report["checks"]
    )


def test_fresh_unprivileged_verifier_authenticates_handoff() -> None:
    steps = _steps("verify")
    actions = [step["uses"] for step in steps if "uses" in step]
    checkout = steps[0]
    validate = _step("validate", "verify")["run"]

    assert checkout["uses"] == CHECKOUT
    assert checkout["with"]["repository"] == "rrrozhd/zeroth"
    assert "validate-handoff" in validate and "--image-archive evidence/image.tar" in validate
    assert "--verdict evidence/verdict.json" in validate
    assert "$GITHUB_OUTPUT" in validate
    assert "npm ci" not in "\n".join(step.get("run", "") for step in steps)
    assert any(action.startswith("actions/download-artifact@") for action in actions)
    assert any(action.startswith("actions/upload-artifact@") for action in actions)


def test_hidden_unprivileged_handoff_is_included_in_artifact() -> None:
    upload = next(
        step for step in _steps() if step["name"] == "Upload unprivileged certification handoff"
    )

    assert upload["with"]["path"] == "app/.app-certification"
    assert upload["with"]["include-hidden-files"] is True


def test_privileged_job_uses_only_authenticated_verifier_outputs() -> None:
    steps = _steps("attest")
    actions = [step["uses"] for step in steps if "uses" in step]
    checkout = steps[0]
    authenticate = _step("authenticate", "attest")["run"]
    attest = _step("provenance", "attest")

    assert checkout["uses"] == CHECKOUT
    assert checkout["with"]["repository"] == "rrrozhd/zeroth"
    assert "sha256sum" in authenticate and "needs.verify.outputs.verdict_sha256" in authenticate
    assert attest["with"]["subject-name"] == "${{ needs.verify.outputs.image_reference }}"
    assert attest["with"]["subject-digest"] == "${{ needs.verify.outputs.image_digest }}"
    assert attest["with"]["predicate-path"] == "evidence/attestation-predicate.json"
    assert any(action.startswith("actions/download-artifact@") for action in actions)
    assert any(action.startswith("actions/attest@") for action in actions)


def test_finalizer_reissues_verdict_for_the_signed_report() -> None:
    finalize = _step("finalize-attestation", "attest")["run"]

    assert "--image-archive evidence/image.tar" in finalize
    assert "--source-archive evidence/source.tar" in finalize
    assert "--verdict evidence/verdict.json" in finalize


def test_all_external_actions_are_commit_pinned() -> None:
    references = [
        step["uses"]
        for job in ("certify", "verify", "attest")
        for step in _steps(job)
        if "uses" in step
    ]
    for action, pin in ACTION_PINS.items():
        matches = [reference for reference in references if reference.startswith(f"{action}@")]
        assert matches and all(reference == f"{action}@{pin}" for reference in matches)


def test_vendor_dd_reference_uses_structured_semantic_targets() -> None:
    declaration = load_declaration(DECLARATION)
    raw = json.loads(DECLARATION.read_text(encoding="utf-8"))

    assert declaration.schema_version == 2
    assert declaration.zeroth_version == "0.23.9.6"
    assert "checks" not in raw
    assert raw["targets"]["contracts"] == "apps.vendor_dd.contracts:CONTRACTS"
    assert raw["targets"]["policy_guard"] == "apps.vendor_dd.entrypoint:build_policy_guard"
    assert len(raw["targets"]["graph_builders"]) == 3
    assert raw["smoke"]["headers_from_env"] == {"X-API-Key": "APP_CERTIFICATION_API_KEY"}


def test_vendor_dd_container_healthcheck_parses_readiness_payload() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    copy_lines = [line for line in dockerfile.splitlines() if line.startswith("COPY ")]

    assert "zeroth_core-0.23.9.6-py3-none-any.whl" in dockerfile
    assert copy_lines == [
        "COPY .zeroth-certifier/requirements-image.txt /tmp/requirements-image.txt",
        "COPY .zeroth-certifier/zeroth_core-0.23.9.6-py3-none-any.whl /opt/zeroth/",
        "COPY apps/vendor_dd /opt/vendor/app/apps/vendor_dd",
    ]
    assert "apps.vendor_dd.certification_healthcheck" in dockerfile
    assert "ZEROTH_REGULUS__ENABLED=true" in dockerfile
    assert "ZEROTH_ARTIFACT_STORE__FILESYSTEM_BASE_DIR=/data/artifacts" in dockerfile
    assert "ECP_DATABASE_URL=sqlite:////data/vendor_dd_econ.sqlite" in dockerfile
    assert 'CMD ["python", "-m", "apps.vendor_dd.certification_entrypoint"]' in dockerfile


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _wait_for_readiness(process: subprocess.Popen[str], port: int) -> bool:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and process.poll() is None:
        try:
            with urlopen(f"http://127.0.0.1:{port}/health/ready", timeout=1) as response:
                if json.load(response).get("status") == "ok":
                    return True
        except OSError:
            time.sleep(0.2)
    return False


def test_vendor_dd_seeded_service_reaches_health_readiness(tmp_path: Path) -> None:
    port = _free_port()
    env = {
        **os.environ,
        "HOST": "127.0.0.1",
        "PORT": str(port),
        "ZEROTH_DATABASE__BACKEND": "sqlite",
        "ZEROTH_DATABASE__SQLITE_PATH": str(tmp_path / "vendor-dd.sqlite"),
        "ZEROTH_ARTIFACT_STORE__FILESYSTEM_BASE_DIR": str(tmp_path / "artifacts"),
        "ZEROTH_REGULUS__ENABLED": "true",
        "ZEROTH_REGULUS__BASE_URL": f"http://127.0.0.1:{port}/regulus/v1",
        "ZEROTH_WEBHOOK__ENABLED": "false",
        "ZEROTH_APPROVAL_SLA__ENABLED": "false",
        "ZEROTH_REDIS__MODE": "disabled",
        "ECP_DATABASE_URL": f"sqlite:///{tmp_path / 'econ.sqlite'}",
        "ECP_CONNECTOR_SPOOL_ROOT": str(tmp_path / "connector-spool"),
    }
    seeded = subprocess.run(
        [sys.executable, "-m", "apps.vendor_dd.seed"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert seeded.returncode == 0, seeded.stderr[-1000:]
    process = subprocess.Popen(
        [sys.executable, "-m", "apps.vendor_dd.entrypoint"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready = _wait_for_readiness(process, port)
    finally:
        if process.poll() is None:
            process.terminate()
        stdout, stderr = process.communicate(timeout=10)
    assert ready, f"vendor-dd readiness failed: {stdout[-1000:]} {stderr[-1000:]}"
    for runner in ("chat-analyst", "dim-analyst", "report", "screen"):
        assert runner in stdout
