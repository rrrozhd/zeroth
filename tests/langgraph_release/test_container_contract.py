from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_container_and_compatibility_contract() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
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
    assert 'ARG ZEROTH_EXTRAS="langgraph,langgraph-gateway"' in dockerfile
    assert dockerfile.count("python:3.12.13-slim-bookworm") == 2
    assert "org.opencontainers.image.version=0.16.2.8" in dockerfile
    assert "memory-pg,langgraph,langgraph-gateway" in compose
    build_step = next(
        step
        for step in workflow_config["jobs"]["container-evidence"]["steps"]
        if step.get("name") == "Build release image"
    )
    assert build_step["run"] == (
        "docker build --build-arg "
        "ZEROTH_EXTRAS=memory-pg,langgraph,langgraph-gateway "
        "-t zeroth-core:${{ github.ref_name }} ."
    )
    assert "langgraph-fixture:" in compose
    assert 'ZEROTH_LANGGRAPH_GATEWAY__ENABLED: "true"' in compose
    assert "ZEROTH_LANGGRAPH_GATEWAY__UPSTREAM_URL:" in compose
    assert "ZEROTH_LANGGRAPH_GATEWAY__UPSTREAM_AUDIENCE:" in compose
    assert "ZEROTH_LANGGRAPH_GATEWAY__DEPLOYMENT_REF:" in compose
    assert "stop_grace_period" in compose and "/health/ready" in compose
    assert "actions/attest@v4" in workflow
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

    assert compatibility["adapter_version"] == "1.0"
    assert compatibility["tested"]["langgraph"] == "1.2.9"
    assert compatibility["tested"]["agent_server"] == "0.11.1"
    assert {"gateway", "adapter"} <= compatibility["deployment_artifacts"].keys()
    assert (
        "langgraph-checkpoint-sqlite>=3.0,<4"
        in compatibility["deployment_artifacts"]["adapter"]["dependencies"]
    )


def test_installed_image_packages_are_compared_with_compatibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "release/langgraph"))
    from runtime_smoke import installed_package_evidence

    compatibility_path = ROOT / "release/langgraph/compatibility.json"
    compatibility = json.loads(compatibility_path.read_text(encoding="utf-8"))
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

    monkeypatch.setattr("runtime_smoke.subprocess.run", fake_run)
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

    packages["langgraph"] = "0.0.0"
    with pytest.raises(RuntimeError, match="installed image packages"):
        installed_package_evidence(
            f"zeroth-core:v{resolved['zeroth_core']}", compatibility_path, image_path
        )
