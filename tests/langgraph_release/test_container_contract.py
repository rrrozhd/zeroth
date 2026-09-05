from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]

IMAGE_EXPORT_COMMAND = (
    "uv",
    "export",
    "--locked",
    "--no-dev",
    "--extra",
    "memory-pg",
    "--extra",
    "langgraph",
    "--extra",
    "langgraph-gateway",
    "--extra",
    "regulus",
    "--extra",
    "cloud",
    "--no-emit-project",
    "--no-annotate",
    "--no-header",
)


def test_container_and_compatibility_contract() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    compose_config = yaml.safe_load(compose)
    workflow = (ROOT / ".github/workflows/release-zeroth-core.yml").read_text(encoding="utf-8")
    workflow_config = yaml.safe_load(workflow)
    runtime = (ROOT / "release/langgraph/runtime_smoke.py").read_text(encoding="utf-8")
    manifest = json.loads(
        (ROOT / "release/langgraph/release-manifest.json").read_text(encoding="utf-8")
    )
    compatibility = json.loads(
        (ROOT / "release/langgraph/compatibility.json").read_text(encoding="utf-8")
    )

    assert "--uid 10001" in dockerfile and "USER zeroth" in dockerfile
    assert "HEALTHCHECK" in dockerfile and "/health/ready" in dockerfile
    assert "io.zeroth.langgraph.adapter.version=1.0" in dockerfile
    assert "io.zeroth.langgraph.compatibility.langgraph=1.2.9" in dockerfile
    assert "io.zeroth.langgraph.compatibility.agent-server=0.11.1" in dockerfile
    assert "ARG ZEROTH_EXTRAS" not in dockerfile
    assert dockerfile.count("python:3.12.13-slim-bookworm") == 1
    evidence_version = compatibility["release"]
    benchmark = json.loads(
        (ROOT / "release/langgraph/benchmark-evidence.json").read_text(encoding="utf-8")
    )
    benchmark_source = (ROOT / "release/langgraph/langgraph_benchmark.py").read_text(
        encoding="utf-8"
    )

    assert compose_config["services"]["zeroth"]["image"] == (
        f"zeroth-core:${{ZEROTH_IMAGE_TAG:-{evidence_version}}}"
    )
    assert compatibility["resolved"]["zeroth_core"] == evidence_version
    assert manifest["release"] == evidence_version
    assert benchmark["release"] == evidence_version
    assert f'CURRENT_RELEASE = "{evidence_version}"' in benchmark_source
    assert "requirements-image.txt" in dockerfile
    build_step = next(
        step
        for step in workflow_config["jobs"]["container-evidence"]["steps"]
        if step.get("name") == "Build release image"
    )
    assert build_step["run"] == "docker build -t zeroth-core:${{ github.ref_name }} ."
    assert "langgraph-fixture:" in compose
    assert 'ZEROTH_LANGGRAPH_GATEWAY__ENABLED: "true"' in compose
    assert "ZEROTH_LANGGRAPH_GATEWAY__UPSTREAM_URL:" in compose
    assert "ZEROTH_LANGGRAPH_GATEWAY__UPSTREAM_AUDIENCE:" in compose
    assert "ZEROTH_LANGGRAPH_GATEWAY__DEPLOYMENT_REF:" in compose
    assert "stop_grace_period" in compose and "/health/ready" in compose
    assert "actions/attest@1e69f48acb82d1966a394da916b4c1698aa569d6 # v4" in workflow
    assert "attestations: write" in workflow and "id-token: write" in workflow
    assert "artifact-metadata: write" in workflow
    assert "sbom-path" in workflow and "smoke" in workflow.lower()
    assert "--junitxml=release/langgraph/junit.xml" in workflow
    assert "name: langgraph-junit" in workflow
    assert "output-file: release/langgraph/image.spdx.json" in workflow
    assert "steps.provenance.outputs.bundle-path" in workflow
    assert "subject-path: zeroth-core-image.tar" in workflow
    assert "gh attestation verify zeroth-core-image.tar" in workflow
    assert '--repo "$GITHUB_REPOSITORY"' in workflow
    assert "release/langgraph/attestation-verification.json" in workflow
    assert workflow.index("gh attestation verify") < workflow.index("validate --phase final")
    assert "image-evidence" in workflow
    assert "image-packages" in workflow
    assert "--sbom release/langgraph/image.spdx.json" in workflow
    assert "--artifact zeroth-core-image.tar" in workflow
    assert "subject-name: zeroth-core" in workflow
    assert "subject-digest: ${{ steps.images.outputs.digest }}" in workflow
    assert "validate --phase final" in workflow
    assert '"docker", "run"' in runtime and "importlib.metadata" in runtime
    assert "hashlib.sha256" in runtime
    assert "release/langgraph/image-packages.json" in manifest["evidence"]["security"]["artifacts"]
    assert (
        "release/langgraph/attestation-verification.json"
        in manifest["evidence"]["security"]["artifacts"]
    )
    assert "zeroth-core seed-demo" in workflow
    assert "timeout 15" in workflow
    assert "Input should be 'sqlite' or 'postgres'" in workflow
    assert "gateway-smoke" in workflow
    assert "release-slow" in workflow
    assert "push: true" not in workflow

    assert compatibility["adapter_version"] == "2.0"
    assert compatibility["tested"]["langgraph"] == "1.2.9"
    assert compatibility["tested"]["agent_server"] == "0.11.1"
    assert {"gateway", "adapter"} <= compatibility["deployment_artifacts"].keys()
    assert (
        "langgraph-checkpoint-sqlite>=3.0,<4"
        in compatibility["deployment_artifacts"]["adapter"]["dependencies"]
    )


def test_active_image_version_matches_packaged_project() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    # Retained compatibility evidence describes its original release. The
    # image built today must identify the package actually being shipped.
    assert f"org.opencontainers.image.version={project['project']['version']}" in dockerfile


def test_installed_image_packages_are_compared_with_compatibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from release.langgraph.runtime_smoke import installed_package_evidence

    compatibility = json.loads(
        (ROOT / "release/langgraph/compatibility.json").read_text(encoding="utf-8")
    )
    # Exercise a new image version without rewriting retained historical evidence.
    compatibility["release"] = "9.8.7"
    compatibility["resolved"]["zeroth_core"] = "9.8.7"
    compatibility_path = tmp_path / "compatibility.json"
    compatibility_path.write_text(json.dumps(compatibility), encoding="utf-8")
    resolved = compatibility["resolved"]
    packages = {
        "zeroth-core": resolved["zeroth_core"],
        "langchain": resolved["langchain"],
        "langgraph": resolved["langgraph"],
        "langgraph-checkpoint-sqlite": resolved["langgraph_checkpoint_sqlite"],
        "langgraph-sdk": resolved["langgraph_sdk"],
        "httpx": resolved["httpx"],
        "websockets": resolved["websockets"],
    }
    labels = {
        "org.opencontainers.image.version": resolved["zeroth_core"],
        "io.zeroth.langgraph.adapter.version": compatibility["adapter_version"],
        "io.zeroth.langgraph.compatibility.langgraph": resolved["langgraph"],
        "io.zeroth.langgraph.compatibility.agent-server": resolved["agent_server"],
    }

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        output = (
            json.dumps(packages)
            if command[1] == "run"
            else json.dumps([{"Config": {"Labels": labels}}])
        )
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    monkeypatch.setattr("release.langgraph.runtime_smoke.subprocess.run", fake_run)
    image_path = tmp_path / "images.json"
    image_path.write_text(
        json.dumps(
            {
                "images": [
                    {
                        "reference": f"zeroth-core:v{resolved['zeroth_core']}",
                        "digest": "sha256:" + "d" * 64,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    evidence = installed_package_evidence(
        f"zeroth-core:v{resolved['zeroth_core']}", compatibility_path, image_path
    )
    assert evidence["packages"] == packages
    assert evidence["release"] == packages["zeroth-core"]

    packages["langgraph"] = "0.0.0"
    with pytest.raises(RuntimeError, match="installed image packages"):
        installed_package_evidence(
            f"zeroth-core:v{resolved['zeroth_core']}", compatibility_path, image_path
        )


def test_the_governed_upstream_is_a_real_agent_server_not_an_imitation() -> None:
    """The gateway's compatibility check is only worth running against real software.

    What this replaced served `/ok`, `/info` and `/openapi.json` from literals and
    rebuilt its OpenAPI document from `tests/langgraph_gateway/fixtures/
    openapi-0.11.1.operations.json` — the same fixture the gateway's fingerprint pin was
    derived from. So the pin was compared against its own answer key and the stack
    reported a supported Agent Server with none present. An imitation that recites the
    expected fingerprint is worse than no upstream, because it reports success.
    """
    root = Path(__file__).resolve().parents[2]
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    smoke = (root / "release/langgraph/runtime_smoke.py").read_text(encoding="utf-8")
    dockerfile = (root / "release/langgraph/Dockerfile").read_text(encoding="utf-8")

    # The upstream is built, and built from the pinned Agent Server package.
    assert "release/langgraph/Dockerfile" in compose
    assert "mock-upstream" not in compose
    assert "langgraph-api==0.11.1" in dockerfile
    assert "run_server" in dockerfile

    # Nothing reconstructs the fingerprint's own source fixture any more.
    assert "openapi-0.11.1.operations.json" not in smoke
    assert "AgentServerFixtureHandler" not in smoke
    assert "serve_shell_agent_server" in smoke

    # And the graph it serves is a real compiled StateGraph.
    graph_source = (root / "release/langgraph/shell_graph.py").read_text(encoding="utf-8")
    assert "StateGraph" in graph_source and ".compile()" in graph_source


def test_image_dependencies_are_hash_locked(tmp_path: Path) -> None:
    requirements = ROOT / "requirements-image.txt"
    assert requirements.is_file(), "requirements-image.txt is not checked in"

    exported = tmp_path / "requirements-image.txt"
    result = subprocess.run(
        [*IMAGE_EXPORT_COMMAND, "--output-file", str(exported)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert requirements.read_bytes() == exported.read_bytes()

    locked = requirements.read_text(encoding="utf-8")
    assert " --hash=sha256:" in locked
    assert "-e " not in locked and "zeroth-core" not in locked

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY requirements-image.txt" in dockerfile
    assert "pip install --no-cache-dir" in dockerfile
    assert "--require-hashes" in dockerfile
    assert "--only-binary=:all:" in dockerfile
    assert "-r /tmp/requirements-image.txt" in dockerfile


def test_compose_services_are_hardened() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))

    assert set(compose["services"]) == {"zeroth", "langgraph-fixture", "db"}
    for name, service in compose["services"].items():
        assert service.get("read_only") is True, f"{name} root filesystem is writable"
        assert "ALL" in service.get("cap_drop", []), f"{name} retains Linux capabilities"
        assert "no-new-privileges:true" in service.get("security_opt", []), (
            f"{name} can gain privileges"
        )
        assert isinstance(service.get("pids_limit"), int) and service["pids_limit"] > 0, (
            f"{name} has no positive PID limit"
        )

    db = compose["services"]["db"]
    assert db.get("user") == "postgres"
    assert "zeroth-pg:/var/lib/postgresql/data" in db["volumes"]
    db_tmpfs = {str(path).split(":", 1)[0] for path in db.get("tmpfs", [])}
    assert {"/tmp", "/var/run/postgresql"} <= db_tmpfs

    zeroth = compose["services"]["zeroth"]
    assert zeroth["environment"]["ZEROTH_ARTIFACT_STORE__FILESYSTEM_BASE_DIR"] == (
        "/data/artifacts"
    )
    assert "zeroth-data:/data" in zeroth["volumes"]
    assert "zeroth-data" in compose["volumes"]


def test_container_docs_build_the_candidate_wheel_first() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    deployment = (ROOT / "docs/how-to/deployment/langgraph-release.md").read_text(encoding="utf-8")

    assert "uv build --wheel" in readme
    assert readme.index("uv build --wheel") < readme.index("docker build -t zeroth-core .")
    assert "uv build --wheel" in deployment
    assert deployment.index("uv build --wheel") < deployment.index("docker compose build")
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    assert f"Zeroth `{version}`" in deployment
    # The guide installs from a checkout at the release tag (PyPI serves only a
    # stale 0.1.0 placeholder), so the version is pinned via the tag, not an
    # uninstallable pip pin.
    assert f"git checkout v{version}" in deployment


def test_release_image_consumes_and_compares_the_candidate_wheel() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY dist/zeroth_core-*.whl /opt/zeroth/wheel/" in dockerfile
    assert "pip install --no-cache-dir --no-deps /opt/zeroth/wheel/zeroth_core-*.whl" in dockerfile
    assert "python -m build" not in dockerfile and "ARG ZEROTH_EXTRAS" not in dockerfile

    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/release-zeroth-core.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["container-evidence"]["steps"]
    build_index = next(
        i for i, step in enumerate(steps) if step.get("name") == "Build release image"
    )
    dist_downloads = [
        (i, step) for i, step in enumerate(steps) if step.get("with", {}).get("name") == "dist"
    ]
    assert len(dist_downloads) == 1
    assert dist_downloads[0][0] < build_index
    assert dist_downloads[0][1]["with"]["path"] == "dist/"
    assert steps[build_index]["run"] == "docker build -t zeroth-core:${{ github.ref_name }} ."

    comparison = next(
        step for step in steps if step.get("name") == "Compare image wheel with release candidate"
    )
    assert steps.index(comparison) > build_index
    assert "docker cp" in comparison["run"]
    assert "/opt/zeroth/wheel" in comparison["run"]
    assert "cmp " in comparison["run"]
