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
    assert "memory-pg,langgraph,langgraph-gateway" in compose
    assert "stop_grace_period" in compose and "/health/ready" in compose
    assert "actions/attest@v4" in workflow
    assert "attestations: write" in workflow and "id-token: write" in workflow
    assert "sbom-path" in workflow and "smoke" in workflow.lower()
    assert "push: true" not in workflow

    assert compatibility["adapter_version"] == "1.0"
    assert compatibility["tested"]["langgraph"] == "1.2.9"
    assert compatibility["tested"]["agent_server"] == "0.11.1"
    assert {"gateway", "adapter"} <= compatibility["deployment_artifacts"].keys()
