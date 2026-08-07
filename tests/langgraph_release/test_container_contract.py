from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_container_and_compatibility_contract() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/release-zeroth-core.yml").read_text(encoding="utf-8")
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
    assert "org.opencontainers.image.version=0.16.2.1" in dockerfile
    assert "memory-pg,langgraph,langgraph-gateway" in compose
    assert "langgraph-fixture:" in compose
    assert "ZEROTH_LANGGRAPH_GATEWAY__ENABLED: \"true\"" in compose
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
    assert "image-evidence" in workflow
    assert "validate --phase final" in workflow
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
    assert "langgraph-checkpoint-sqlite>=3.0,<4" in compatibility["deployment_artifacts"][
        "adapter"
    ]["dependencies"]
