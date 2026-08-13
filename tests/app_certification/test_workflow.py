from __future__ import annotations

import json
from pathlib import Path

import yaml

from release.app_certification import MANDATORY_CHECKS, load_declaration


ROOT = Path(__file__).parents[2]
REUSABLE = ROOT / ".github/workflows/app-certification.yml"
CALLER = ROOT / ".github/workflows/vendor-dd-certification.yml"
DECLARATION = ROOT / "apps/vendor_dd/certification.json"
DOCKERFILE = ROOT / "apps/vendor_dd/Dockerfile.certification"
PERMISSIONS = {"contents": "read", "attestations": "write", "id-token": "write"}
CHECKOUT = "actions/checkout@11d5960a326750d5838078e36cf38b85af677262"
ACTION_PINS = {
    "actions/checkout": "11d5960a326750d5838078e36cf38b85af677262",
    "astral-sh/setup-uv": "11f9893b081a58869d3b5fccaea48c9e9e46f990",
    "anchore/sbom-action": "e22c389904149dbc22b58101806040fa8d37a610",
    "actions/attest": "1e69f48acb82d1966a394da916b4c1698aa569d6",
    "actions/upload-artifact": "ea165f8d65b6e75b540449e92b4886f43607fa02",
}


def _load_workflow(path: Path) -> dict:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["on"] = document.pop(True, document.get("on"))
    return document


def _steps() -> list[dict]:
    return _load_workflow(REUSABLE)["jobs"]["certify"]["steps"]


def _step(step_id: str) -> dict:
    return next(step for step in _steps() if step.get("id") == step_id)


def test_reusable_workflow_declares_pinned_certifier_input() -> None:
    workflow = _load_workflow(REUSABLE)

    assert set(workflow["on"]) == {"workflow_call", "workflow_dispatch"}
    assert workflow["on"]["workflow_call"]["inputs"]["zeroth_ref"]["required"] is True
    assert workflow["on"]["workflow_dispatch"]["inputs"]["zeroth_ref"]["required"] is True


def test_vendor_dd_caller_uses_the_reusable_workflow_as_a_job() -> None:
    caller = _load_workflow(CALLER)
    job = caller["jobs"]["certify-vendor-dd"]

    assert caller["permissions"] == PERMISSIONS
    assert job["uses"] == "./.github/workflows/app-certification.yml"
    assert "steps" not in job and "runs-on" not in job
    assert job["with"]["zeroth_ref"] == "${{ github.sha }}"
    assert job["with"]["declaration_path"] == "apps/vendor_dd/certification.json"


def test_reusable_workflow_builds_two_measured_healthy_boundaries() -> None:
    workflow = _load_workflow(REUSABLE)
    steps = _steps()
    app_checkout, certifier_checkout = steps[:2]
    assert workflow["permissions"] == PERMISSIONS
    assert app_checkout["uses"] == CHECKOUT
    assert app_checkout["with"]["path"] == "app"
    assert certifier_checkout["uses"] == CHECKOUT
    assert certifier_checkout["with"] == {
        "repository": "rrrozhd/zeroth",
        "ref": "${{ inputs.zeroth_ref }}",
        "path": "zeroth",
        "persist-credentials": False,
    }

    prepare = _step("prepare")["run"]
    assert "uv sync --frozen --all-extras" in prepare
    assert "uv build --wheel" in prepare
    assert "npm ci --prefix app/frontend" in prepare
    assert "DECLARED_VERSION" in prepare and "CERTIFIER_VERSION" in prepare
    assert "git -C app rev-parse HEAD" in prepare
    assert "git -C zeroth rev-parse HEAD" in prepare
    assert "ZEROTH_COMMIT" in prepare and '"$ZEROTH_COMMIT" == "$ZEROTH_REF"' in prepare
    assert "realpath -e" in prepare and '"$GITHUB_WORKSPACE/app/"*' in prepare

    image = _step("image")["run"]
    assert image.count("docker build") == 1
    assert "docker image inspect" in image
    assert "spdx" not in image.lower()

    start = _step("containers")["run"]
    runs = [line for line in start.splitlines() if line.strip().startswith("docker run")]
    assert len(runs) == 2
    assert "app-cert-packaged" in start and "app-cert-ephemeral" in start
    assert "app-cert-packaged-net" in start and "app-cert-ephemeral-net" in start
    assert "--tmpfs /data:rw,noexec,nosuid,size=256m,uid=10001,gid=10001" in start
    assert start.count("--env APP_CERTIFICATION_API_KEY") == 2
    assert "VENDOR_DD_API_KEY=" not in start
    assert start.count('"$IMAGE_ID"') == 2
    assert _step("containers")["env"]["IMAGE_ID"] == "${{ steps.image.outputs.digest }}"

    health_index = next(index for index, step in enumerate(steps) if step.get("id") == "health")
    certify_index = next(index for index, step in enumerate(steps) if step.get("id") == "certify")
    health = _step("health")["run"]
    certification = _step("certify")["run"]
    assert health_index < certify_index
    assert health.count("wait_healthy") >= 3
    assert "http://127.0.0.1:18080" in certification
    assert "http://127.0.0.1:18081" in certification
    assert "python -m release.app_certification" in certification
    assert "${{" not in certification
    assert _step("certify")["env"] == {
        "PYTHONPATH": "${{ github.workspace }}/zeroth",
        "DECLARATION_PATH": "${{ inputs.declaration_path }}",
        "APP_COMMIT": "${{ steps.prepare.outputs.app_commit }}",
        "IMAGE_DIGEST": "${{ steps.image.outputs.digest }}",
    }


def test_workflow_retains_pinned_sbom_provenance_and_failure_diagnostics() -> None:
    steps = _steps()
    action_refs = [step["uses"] for step in steps if "uses" in step]

    for action, pin in ACTION_PINS.items():
        matches = [ref for ref in action_refs if ref.startswith(f"{action}@")]
        assert matches and all(ref == f"{action}@{pin}" for ref in matches)

    sbom = _step("sbom")
    assert sbom["with"]["format"] == "spdx-json"
    assert sbom["with"]["image"] == "${{ steps.prepare.outputs.image_reference }}"
    assert sbom["with"]["output-file"] == "app/.app-certification/image.spdx.json"

    provenance = _step("provenance")
    assert provenance["with"] == {
        "subject-name": "${{ steps.prepare.outputs.image_reference }}",
        "subject-digest": "${{ steps.image.outputs.digest }}",
    }
    retain_step = _step("retain-evidence")
    retain = retain_step["run"]
    assert 'safe_destination "$SBOM_REL"' in retain
    assert 'safe_destination "$PROVENANCE_REL"' in retain
    assert 'cp "$PROVENANCE_BUNDLE"' in retain
    assert "relative_to(root)" in retain
    assert "app/.app-certification/root" in retain
    assert "${{" not in retain
    assert retain_step["env"] == {
        "SBOM_REL": "${{ steps.prepare.outputs.sbom_path }}",
        "PROVENANCE_REL": "${{ steps.prepare.outputs.provenance_path }}",
        "PROVENANCE_BUNDLE": "${{ steps.provenance.outputs.bundle-path }}",
    }

    logs = _step("logs")
    cleanup = _step("cleanup")
    upload = _step("diagnostics")
    assert logs["if"] == "${{ always() }}"
    assert "docker logs app-cert-packaged" in logs["run"]
    assert "docker logs app-cert-ephemeral" in logs["run"]
    assert cleanup["if"] == "${{ always() }}"
    assert "docker stop app-cert-packaged app-cert-ephemeral" in cleanup["run"]
    assert "docker rm app-cert-packaged app-cert-ephemeral" in cleanup["run"]
    assert upload["if"] == "${{ always() }}"
    assert upload["uses"] == f"actions/upload-artifact@{ACTION_PINS['actions/upload-artifact']}"
    assert upload["with"]["path"] == "app/.app-certification"
    assert "app/.app-certification/declaration.json" in _step("prepare")["run"]

    scripts = "\n".join(step.get("run", "") for step in steps)
    assert "docker push" not in scripts
    assert "kubectl" not in scripts


def test_vendor_dd_reference_is_ready_and_exercises_every_real_boundary() -> None:
    declaration = load_declaration(DECLARATION)
    source = DECLARATION.read_text(encoding="utf-8")
    raw = json.loads(source)
    commands = [tuple(raw["checks"][name]) for name in MANDATORY_CHECKS]
    joined = "\n".join(" ".join(command) for command in commands)

    assert declaration.zeroth_version == "0.23.9"
    assert declaration.lock_path == "uv.lock"
    assert raw["dockerfile"] == "apps/vendor_dd/Dockerfile.certification"
    assert set(raw["checks"]) == set(MANDATORY_CHECKS)
    assert len(commands) == 14 and len(set(commands)) == 14
    assert all(command and all(isinstance(arg, str) and arg for arg in command) for command in commands)
    for real_surface in (
        "apps.vendor_dd.graphs",
        "GraphValidator",
        "get_settings",
        "apps.vendor_dd.contracts",
        "run_migrations",
        "build_policy_guard",
        "scripts/check_frontend_api.py",
        "docker inspect",
    ):
        assert real_surface in joined
    assert raw["checks"]["frontend-api"] == [
        "python",
        "scripts/check_frontend_api.py",
        "--frontend",
        "frontend",
    ]
    assert raw["smoke"]["headers_from_env"] == {
        "X-API-Key": "APP_CERTIFICATION_API_KEY"
    }
    assert "vendor-dd-ops-key" not in source
    assert raw["smoke"]["expected_status"] == 202
    assert raw["smoke"]["expected_json"] == {
        "status": "queued",
        "deployment_ref": "vendor-dd",
    }

    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    copy_lines = [line for line in dockerfile.splitlines() if line.startswith("COPY ")]
    assert ".zeroth-certifier/requirements-image.txt" in dockerfile
    assert ".zeroth-certifier/zeroth_core-0.23.9-py3-none-any.whl" in dockerfile
    assert copy_lines == [
        "COPY .zeroth-certifier/requirements-image.txt /tmp/requirements-image.txt",
        "COPY .zeroth-certifier/zeroth_core-0.23.9-py3-none-any.whl /opt/zeroth/",
        "COPY apps/vendor_dd /opt/vendor/app/apps/vendor_dd",
    ]
    assert "USER vendor-dd" in dockerfile
    assert "HEALTHCHECK" in dockerfile and "/health/ready" in dockerfile
    assert 'CMD ["python", "-m", "apps.vendor_dd.certification_entrypoint"]' in dockerfile
