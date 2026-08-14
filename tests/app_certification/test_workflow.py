from __future__ import annotations

import json
from pathlib import Path

import yaml

from release.app_certification import load_declaration


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


def test_reusable_workflow_has_unprivileged_candidate_and_privileged_attestation_jobs() -> None:
    workflow = _load()
    candidate = workflow["jobs"]["certify"]
    attestation = workflow["jobs"]["attest"]

    assert set(workflow["on"]) == {"workflow_call", "workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    assert candidate["permissions"] == {"contents": "read"}
    assert attestation["permissions"] == PRIVILEGED
    assert attestation["needs"] == "certify"
    assert attestation["if"] == "${{ needs.certify.result == 'success' }}"


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
    assert "npm ci --prefix app/frontend" in prepare
    assert "app/.venv/bin/python -m release.app_certification run" in _step("certify")["run"]
    assert not any(action.startswith("actions/attest@") for action in actions)
    assert "docker push" not in scripts and "kubectl" not in scripts


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


def test_every_pre_certification_failure_gets_a_canonical_report() -> None:
    finalizer = _step("finalizer")
    script = finalizer["run"]

    assert finalizer["if"] == "${{ always() }}"
    assert "workflow-stages.json" in script
    assert '"status": "failed"' in script
    assert '"candidate": None' in script and '"evidence": None' in script
    for name in ("prepare", "image", "containers", "health", "certify"):
        assert name in script.lower()
    for name in ("graph", "contracts", "migrations", "frontend-api", "provenance"):
        assert name in script


def test_privileged_job_validates_immutable_handoff_before_attesting() -> None:
    steps = _steps("attest")
    actions = [step["uses"] for step in steps if "uses" in step]
    checkout = steps[0]
    validate = _step("validate", "attest")["run"]
    attest = _step("provenance", "attest")

    assert checkout["uses"] == CHECKOUT
    assert checkout["with"]["repository"] == "rrrozhd/zeroth"
    assert all(
        "app" not in step.get("name", "").lower() or "handoff" in step.get("name", "").lower()
        for step in steps
    )
    assert "validate-handoff" in validate and "--image-archive evidence/image.tar" in validate
    assert attest["with"]["subject-digest"] == "${{ steps.validate.outputs.image_digest }}"
    assert attest["with"]["predicate-path"] == "evidence/attestation-predicate.json"
    assert any(action.startswith("actions/download-artifact@") for action in actions)
    assert any(action.startswith("actions/attest@") for action in actions)


def test_all_external_actions_are_commit_pinned() -> None:
    references = [
        step["uses"] for job in ("certify", "attest") for step in _steps(job) if "uses" in step
    ]
    for action, pin in ACTION_PINS.items():
        matches = [reference for reference in references if reference.startswith(f"{action}@")]
        assert matches and all(reference == f"{action}@{pin}" for reference in matches)


def test_vendor_dd_reference_uses_structured_semantic_targets() -> None:
    declaration = load_declaration(DECLARATION)
    raw = json.loads(DECLARATION.read_text(encoding="utf-8"))

    assert declaration.schema_version == 2
    assert declaration.zeroth_version == "0.23.9.1"
    assert "checks" not in raw
    assert raw["targets"]["contracts"] == "apps.vendor_dd.contracts:CONTRACTS"
    assert raw["targets"]["policy_guard"] == "apps.vendor_dd.entrypoint:build_policy_guard"
    assert len(raw["targets"]["graph_builders"]) == 3
    assert raw["smoke"]["headers_from_env"] == {"X-API-Key": "APP_CERTIFICATION_API_KEY"}


def test_vendor_dd_container_healthcheck_parses_readiness_payload() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    copy_lines = [line for line in dockerfile.splitlines() if line.startswith("COPY ")]

    assert "zeroth_core-0.23.9.1-py3-none-any.whl" in dockerfile
    assert copy_lines == [
        "COPY .zeroth-certifier/requirements-image.txt /tmp/requirements-image.txt",
        "COPY .zeroth-certifier/zeroth_core-0.23.9.1-py3-none-any.whl /opt/zeroth/",
        "COPY apps/vendor_dd /opt/vendor/app/apps/vendor_dd",
    ]
    assert "apps.vendor_dd.certification_healthcheck" in dockerfile
    assert 'CMD ["python", "-m", "apps.vendor_dd.certification_entrypoint"]' in dockerfile
