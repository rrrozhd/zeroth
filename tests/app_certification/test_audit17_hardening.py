from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from release.app_certification import AppDeclaration
from release.app_certification.checks import run_owned_check
from release.app_certification.scaffold import scaffold_checkout
from tests.app_certification.workflow_fixtures import write_generated_app


ROOT = Path(__file__).parents[2]
WORKFLOW = ROOT / ".github/workflows/app-certification.yml"


def _write_ownership_lease(path: Path, *, daemon_id: str = "daemon-1") -> None:
    from release.app_certification.dependency_sandbox import certification_resources

    run_id = "42-1"
    path.write_text(
        json.dumps(
            {
                "daemon_id": daemon_id,
                "resources": [
                    {"kind": kind, "name": name}
                    for kind, name in certification_resources(run_id)
                ],
                "run_id": run_id,
                "schema_version": 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _missing_resource(argv: list[str]) -> subprocess.CompletedProcess[str]:
    if argv[1:3] == ["buildx", "inspect"]:
        detail = f'ERROR: no builder "{argv[-1]}" found'
    elif argv[1:3] == ["container", "inspect"]:
        detail = f"Error: No such container: {argv[-1]}"
    elif argv[1:3] == ["network", "inspect"]:
        detail = f"Error response from daemon: network {argv[-1]} not found"
    elif argv[1:3] == ["volume", "inspect"]:
        detail = f"Error response from daemon: get {argv[-1]}: no such volume"
    else:
        detail = f"Error: No such image: {argv[-1]}"
    return subprocess.CompletedProcess(argv, 1, "", detail)


def test_workflow_uses_run_owned_image_tags_and_exact_ids() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")

    assert "app-cert-candidate:${{ github.run_id }}-${{ github.run_attempt }}" in source
    assert "app-cert-runtime:${{ github.run_id }}-${{ github.run_attempt }}" in source
    assert "RUNTIME_IMAGE_REFERENCE: ${{ steps.prepare.outputs.image_reference }}" not in source
    assert "candidate_digest=$CANDIDATE_IMAGE_DIGEST" in source
    assert '--candidate-image-id "${{ steps.image.outputs.candidate_digest }}"' in source
    assert '--runtime-image-id "${{ steps.image.outputs.digest }}"' in source
    assert source.count('--ownership "$HANDOFF_ROOT/resource-ownership.json"') == 2


def test_workflow_attempt_identity_is_consistent_across_artifact_handoffs() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for job_name in ("certify", "verify", "attest"):
        for step in workflow["jobs"][job_name]["steps"]:
            name = step.get("with", {}).get("name", "")
            if name.startswith("app-certification-"):
                assert "${{ github.run_id }}-${{ github.run_attempt }}" in name


def test_cleanup_preserves_an_image_when_the_created_id_does_not_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from release.app_certification import dependency_sandbox as sandbox

    removals: list[list[str]] = []
    expected = "sha256:" + "a" * 64
    replacement = "sha256:" + "b" * 64
    candidate = "app-cert-candidate:42-1"
    runtime = "app-cert-runtime:42-1"

    def run(argv: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        if argv[1:3] == ["image", "inspect"] and argv[-1] in {candidate, runtime}:
            return subprocess.CompletedProcess(argv, 0, replacement + "\n", "")
        if "rm" in argv:
            removals.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[1:2] == ["info"]:
            return subprocess.CompletedProcess(argv, 0, "daemon-1\n", "")
        return _missing_resource(argv)

    monkeypatch.setattr(sandbox, "cleanup_build_sandbox", lambda _args: None)
    monkeypatch.setattr(sandbox, "cleanup_dependency_sandbox", lambda _args: None)
    monkeypatch.setattr(sandbox.subprocess, "run", run)
    ownership = tmp_path / "resource-ownership.json"
    _write_ownership_lease(ownership)
    args = SimpleNamespace(
        app_root=tmp_path,
        builder_disk=tmp_path / "builder.img",
        builder_name="app-cert-builder-42-1",
        candidate_image_id=expected,
        container_name="app-cert-dependencies-42-1",
        disk=tmp_path / "dependencies.img",
        docker="docker",
        output=tmp_path / "cleanup.json",
        ownership=ownership,
        run_id="42-1",
        runtime_image_id=expected,
    )

    with pytest.raises(RuntimeError, match="created image|identity|digest"):
        sandbox.cleanup_certification(args)

    assert not any(command[1:3] == ["image", "rm"] for command in removals)
    retained = json.loads(args.output.read_text(encoding="utf-8"))
    assert retained["status"] == "failed"


def test_cleanup_removes_each_created_image_once_and_retains_its_exact_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from release.app_certification import dependency_sandbox as sandbox
    candidate = "app-cert-candidate:42-1"
    runtime = "app-cert-runtime:42-1"
    identities = {
        candidate: "sha256:" + "a" * 64,
        runtime: "sha256:" + "b" * 64,
    }
    removed: list[str] = []

    def run(argv: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        if argv[1:2] == ["info"]:
            return subprocess.CompletedProcess(argv, 0, "daemon-1\n", "")
        if argv[1:3] == ["image", "inspect"] and argv[-1] in identities:
            return subprocess.CompletedProcess(argv, 0, identities[argv[-1]] + "\n", "")
        if argv[1:3] == ["image", "rm"]:
            removed.append(argv[-1])
            identities.pop(argv[-1])
            return subprocess.CompletedProcess(argv, 0, "", "")
        return _missing_resource(argv)
    monkeypatch.setattr(sandbox, "cleanup_build_sandbox", lambda _args: None)
    monkeypatch.setattr(sandbox, "cleanup_dependency_sandbox", lambda _args: None)
    monkeypatch.setattr(sandbox.subprocess, "run", run)
    ownership = tmp_path / "resource-ownership.json"
    _write_ownership_lease(ownership)
    args = SimpleNamespace(
        app_root=tmp_path,
        builder_disk=tmp_path / "builder.img",
        builder_name="app-cert-builder-42-1",
        candidate_image_id="sha256:" + "a" * 64,
        container_name="app-cert-dependencies-42-1",
        disk=tmp_path / "dependencies.img",
        docker="docker",
        output=tmp_path / "cleanup.json",
        ownership=ownership,
        run_id="42-1",
        runtime_image_id="sha256:" + "b" * 64,
    )

    sandbox.cleanup_certification(args)

    assert removed == [candidate, runtime]
    cleanup = json.loads(args.output.read_text(encoding="utf-8"))
    image_resources = [item for item in cleanup["resources"] if item["kind"] == "image"]
    assert [item["created_id"] for item in image_resources] == [
        "sha256:" + "a" * 64,
        "sha256:" + "b" * 64,
    ]


def test_collision_preflight_writes_a_lease_only_after_an_empty_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from release.app_certification import dependency_sandbox as sandbox

    ownership = tmp_path / "resource-ownership.json"
    args = SimpleNamespace(docker="docker", ownership=ownership, run_id="42-1")
    monkeypatch.setattr(sandbox, "_docker_daemon_id", lambda _docker: "daemon-1")
    monkeypatch.setattr(sandbox, "_resource_identity", lambda *_args: None)

    sandbox.check_certification_collisions(args)

    lease = json.loads(ownership.read_text(encoding="utf-8"))
    assert lease["daemon_id"] == "daemon-1"
    assert lease["run_id"] == "42-1"
    assert [(item["kind"], item["name"]) for item in lease["resources"]] == (
        sandbox.certification_resources("42-1")
    )


def test_collision_preflight_does_not_grant_cleanup_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from release.app_certification import dependency_sandbox as sandbox

    ownership = tmp_path / "resource-ownership.json"
    args = SimpleNamespace(docker="docker", ownership=ownership, run_id="42-1")
    monkeypatch.setattr(sandbox, "_docker_daemon_id", lambda _docker: "daemon-1")
    monkeypatch.setattr(
        sandbox,
        "_resource_identity",
        lambda _docker, kind, _name: "pre-existing-id" if kind == "container" else None,
    )

    with pytest.raises(RuntimeError, match="collision"):
        sandbox.check_certification_collisions(args)

    assert not ownership.exists()


def test_cleanup_without_a_collision_lease_preserves_every_named_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from release.app_certification import dependency_sandbox as sandbox

    removals: list[list[str]] = []

    def run(argv: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        if argv[1:2] == ["info"]:
            return subprocess.CompletedProcess(argv, 0, "daemon-1\n", "")
        if "rm" in argv:
            removals.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "Error: No such image: fixture")

    monkeypatch.setattr(
        sandbox,
        "cleanup_build_sandbox",
        lambda _args: (_ for _ in ()).throw(AssertionError("must preserve without lease")),
    )
    monkeypatch.setattr(
        sandbox,
        "cleanup_dependency_sandbox",
        lambda _args: (_ for _ in ()).throw(AssertionError("must preserve without lease")),
    )
    monkeypatch.setattr(sandbox.subprocess, "run", run)
    args = SimpleNamespace(
        app_root=tmp_path,
        builder_disk=tmp_path / "builder.img",
        builder_name="app-cert-builder-42-1",
        candidate_image_id="sha256:" + "a" * 64,
        container_name="app-cert-dependencies-42-1",
        disk=tmp_path / "dependencies.img",
        docker="docker",
        output=tmp_path / "cleanup.json",
        ownership=tmp_path / "missing-ownership.json",
        run_id="42-1",
        runtime_image_id="sha256:" + "b" * 64,
    )

    with pytest.raises(RuntimeError, match="ownership|lease"):
        sandbox.cleanup_certification(args)

    assert removals == []


def test_cleanup_on_a_different_daemon_preserves_every_named_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from release.app_certification import dependency_sandbox as sandbox

    removals: list[list[str]] = []

    def run(argv: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        if argv[1:2] == ["info"]:
            return subprocess.CompletedProcess(argv, 0, "daemon-2\n", "")
        if "rm" in argv:
            removals.append(argv)
        return _missing_resource(argv)

    monkeypatch.setattr(
        sandbox,
        "cleanup_build_sandbox",
        lambda _args: (_ for _ in ()).throw(AssertionError("must preserve across daemons")),
    )
    monkeypatch.setattr(
        sandbox,
        "cleanup_dependency_sandbox",
        lambda _args: (_ for _ in ()).throw(AssertionError("must preserve across daemons")),
    )
    monkeypatch.setattr(sandbox.subprocess, "run", run)
    ownership = tmp_path / "resource-ownership.json"
    _write_ownership_lease(ownership)
    args = SimpleNamespace(
        app_root=tmp_path,
        builder_disk=tmp_path / "builder.img",
        builder_name="app-cert-builder-42-1",
        candidate_image_id="sha256:" + "a" * 64,
        container_name="app-cert-dependencies-42-1",
        disk=tmp_path / "dependencies.img",
        docker="docker",
        output=tmp_path / "cleanup.json",
        ownership=ownership,
        run_id="42-1",
        runtime_image_id="sha256:" + "b" * 64,
    )

    with pytest.raises(RuntimeError, match="daemon|ownership|lease"):
        sandbox.cleanup_certification(args)

    assert removals == []


@pytest.mark.parametrize(
    "diagnostic",
    [
        "permission denied while connecting to the Docker daemon socket",
        "dial unix /var/run/docker.sock: connect: no such file or directory",
    ],
)
def test_cleanup_inspection_distinguishes_daemon_errors_from_not_found(
    diagnostic: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from release.app_certification import dependency_sandbox as sandbox

    monkeypatch.setattr(
        sandbox.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, "", diagnostic),
    )

    with pytest.raises(RuntimeError, match="permission denied|no such file|Docker daemon"):
        sandbox._resource_exists("docker", "image", "app-cert-runtime:42-1")


@pytest.mark.parametrize(
    "resources",
    [
        [],
        [{"absent": True, "kind": "image", "name": "unrelated:latest"}],
    ],
)
def test_cleanup_success_requires_the_exact_nonempty_inventory(
    tmp_path: Path, resources: list[dict[str, object]]
) -> None:
    from release.app_certification.workflow_finalizer import _cleanup_succeeded

    cleanup = tmp_path / "cleanup.json"
    cleanup.write_text(
        json.dumps(
            {
                "daemon_id": "daemon-1",
                "errors": [],
                "resources": resources,
                "run_id": "42-1",
                "schema_version": 1,
                "status": "passed",
            }
        ),
        encoding="utf-8",
    )

    assert _cleanup_succeeded(cleanup) is False


def test_runtime_regulus_probe_is_mandatory_for_both_candidate_modes() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    step = next(
        item for item in workflow["jobs"]["certify"]["steps"] if item.get("id") == "runtime"
    )
    script = step["run"]

    assert "runtime_probe_worker.py" in script
    assert 'docker exec -i "$container" /usr/local/bin/python -I -s -' in script
    assert "probe-regulus" in script
    assert 'probe_mode packaged "$PACKAGED_CONTAINER"' in script
    assert 'probe_mode ephemeral "$EPHEMERAL_CONTAINER"' in script
    assert step["env"]["PACKAGED_CONTAINER"]
    assert step["env"]["EPHEMERAL_CONTAINER"]


def test_exact_runtime_probe_imports_installed_regulus_capabilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from release.app_certification import runtime_probe_worker

    site_packages = tmp_path / "site-packages"
    modules = {
        name: SimpleNamespace(__file__=site_packages / f"module-{index}.py")
        for index, name in enumerate(
            (
                "zeroth.econ.analytics.budget",
                "zeroth.econ.instrumentation",
                "zeroth.econ.plane.main",
            )
        )
    }
    modules["zeroth.econ.plane.main"].app = SimpleNamespace(
        routes=[
            SimpleNamespace(path=path)
            for path in (
                "/v1/auth/token",
                "/v1/budget/tenants/{tenant_id}",
                "/v1/capabilities",
                "/v1/instrumentation/executions",
            )
        ]
    )
    distribution = SimpleNamespace(version="0.23.9.16", locate_file=lambda _name: site_packages)
    monkeypatch.setattr(
        runtime_probe_worker.importlib.metadata, "distribution", lambda _name: distribution
    )
    monkeypatch.setattr(runtime_probe_worker.importlib, "import_module", modules.__getitem__)

    runtime_probe_worker.probe_runtime_extras("0.23.9.16")


def test_regulus_probe_requires_authenticated_budget_and_instrumentation_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from release.app_certification import runtime_probe

    calls: list[tuple[str, str, object | None, dict[str, str]]] = []

    def exchange(url: str, *, method: str, headers: dict[str, str], body, timeout: float):
        assert timeout == 10
        calls.append((url, method, body, headers))
        if url.endswith("/capabilities"):
            return 200, {"id": "app-cert-42-1-packaged"}
        if url.endswith("/implementations"):
            return 200, {"id": "app-cert-42-1-packaged-runtime"}
        if "/budget/tenants/" in url:
            return 200, {"tenant_id": "tenant-acme"}
        if url.endswith("/instrumentation/executions"):
            return 200, {"execution_id": "exec-42-1-packaged"}
        return 200, {
            "budget_cap_usd": 1.0,
            "tenant_id": "tenant-acme",
            "total_cost_usd": 0.001,
        }

    monkeypatch.setenv("APP_CERTIFICATION_API_KEY", "api-key")
    monkeypatch.setenv("APP_CERTIFICATION_REGULUS_TOKEN", "service-token")
    monkeypatch.setattr(runtime_probe, "run_http_exchange", exchange)

    runtime_probe.probe_regulus(
        "http://127.0.0.1:18080/regulus/v1", "tenant-acme", "42-1", "packaged"
    )

    assert [method for _url, method, _body, _headers in calls] == [
        "POST",
        "POST",
        "PUT",
        "POST",
        "GET",
    ]
    assert all(
        headers["Authorization"] == "Bearer service-token" and headers["X-API-Key"] == "api-key"
        for _url, _method, _body, headers in calls
    )


def test_certification_entrypoint_configures_regulus_before_seed_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.vendor_dd import certification_entrypoint

    observed: dict[str, str | None] = {}

    async def seed() -> int:
        return 0

    def import_module(name: str) -> SimpleNamespace:
        if name == "apps.vendor_dd.seed":
            observed.update(
                {
                    key: os.environ.get(key)
                    for key in (
                        "ECP_BASE_URL",
                        "ECP_SERVICE_PRINCIPAL_TENANT_ID",
                        "ZEROTH_REGULUS__BASE_URL",
                    )
                }
            )
            return SimpleNamespace(main=seed)
        return SimpleNamespace(main=lambda: 0)

    for name in observed or (
        "ECP_BASE_URL",
        "ECP_SERVICE_PRINCIPAL_TENANT_ID",
        "ZEROTH_REGULUS__BASE_URL",
    ):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    monkeypatch.setenv("APP_CERTIFICATION_API_KEY", "test-key")
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.setenv("PORT", "8000")
    monkeypatch.setenv("VENDOR_DD_TENANT", "tenant-acme")
    monkeypatch.setattr(certification_entrypoint, "import_module", import_module)

    assert certification_entrypoint.main() == 0
    assert observed == {
        "ECP_BASE_URL": "http://127.0.0.1:8000/regulus/v1",
        "ECP_SERVICE_PRINCIPAL_TENANT_ID": "tenant-acme",
        "ZEROTH_REGULUS__BASE_URL": "http://127.0.0.1:8000/regulus/v1",
    }


def test_importing_direct_entrypoint_does_not_rebind_parent_regulus_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_CERTIFICATION_API_KEY", "test-key")
    for name in ("ECP_BASE_URL", "ECP_SERVICE_PRINCIPAL_TENANT_ID"):
        monkeypatch.delenv(name, raising=False)

    from apps.vendor_dd import entrypoint

    importlib.reload(entrypoint)

    assert "ECP_BASE_URL" not in os.environ
    assert "ECP_SERVICE_PRINCIPAL_TENANT_ID" not in os.environ


def test_scaffold_generates_a_complete_repeatable_semantic_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_generated_app(tmp_path)
    (tmp_path / "frontend").mkdir()
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))

    scaffold_checkout(
        tmp_path,
        app_name="generated",
        module="generated_app",
        zeroth_version="0.23.9.16",
        zeroth_ref="a" * 40,
    )
    declaration = AppDeclaration.model_validate_json(
        (tmp_path / "certification.json").read_text(encoding="utf-8")
    )
    semantic_path = tmp_path / declaration.semantic_path
    manifest = json.loads(semantic_path.read_text(encoding="utf-8"))

    assert manifest["graphs"] and manifest["contracts"]
    assert manifest["policies"] and manifest["capabilities"]
    assert manifest["service_config"]["auth_config"]["api_keys"]
    assert len(manifest["target_sources"]) == 5
    for check in ("graph", "service-config", "contracts", "optional-extras", "policies"):
        run_owned_check(check, tmp_path, declaration)

    command = [
        sys.executable,
        "-m",
        "release.app_certification",
        "generate-semantic",
        "--root",
        str(tmp_path),
        "--declaration",
        str(tmp_path / "certification.json"),
        "--output",
        str(semantic_path),
        "--database-backend",
        "sqlite",
    ]
    first = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    generated = semantic_path.read_bytes()
    second = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    assert second.returncode == 0, second.stderr
    assert semantic_path.read_bytes() == generated


def test_scaffold_generation_failure_does_not_leave_partial_assets(tmp_path: Path) -> None:
    with pytest.raises(ModuleNotFoundError):
        scaffold_checkout(
            tmp_path,
            app_name="broken",
            module="missing_app",
            zeroth_version="0.23.9.16",
            zeroth_ref="a" * 40,
        )

    assert not (tmp_path / "certification.json").exists()
    assert not (tmp_path / "certification.semantic.json").exists()
    assert not (tmp_path / "Dockerfile.certification").exists()
    assert not (tmp_path / ".github/workflows/app-certification.yml").exists()
    assert not (tmp_path / "missing_app/certification_entrypoint.py").exists()
    assert not (tmp_path / "missing_app/certification_healthcheck.py").exists()
    assert not (tmp_path / "missing_app/migrations.py").exists()
