from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from release.live_evaluation.scenario_controller_server import build_parser, compose_app


def test_server_composition_uses_external_fault_state_and_exact_topology(
    tmp_path: Path,
) -> None:
    campaign_id = "evaluation-studio-v1"
    artifact_root = tmp_path / "external"
    campaign_config = tmp_path / "campaign.json"
    campaign_config.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "campaign_id": campaign_id,
                "tenant_id": campaign_id,
                "provider": "openai",
                "model": "openai/gpt-4o-mini",
                "embedding_model": "openai/text-embedding-3-small",
                "vector_backend": "chroma",
                "campaign_budget_usd": "10.00",
                "per_run_cap_usd": "0.25",
                "provider_secret_ref": "llm.openai",
                "artifact_root": str(artifact_root),
                "action_sink_root": str(artifact_root / "action-sink"),
            }
        ),
        encoding="utf-8",
    )
    repository = tmp_path / "repository"
    repository.mkdir()
    args = build_parser().parse_args(
        [
            "--repository-root",
            str(repository),
            "--campaign-config",
            str(campaign_config),
            "--evidence-bundle",
            str(artifact_root / "bundle"),
            "--port",
            "8199",
            *sum(
                (
                    ["--deployment-url", f"{reference}=http://127.0.0.1:{port}"]
                    for reference, port in (
                        (f"{campaign_id}-grounded-researcher-v1", 8101),
                        (f"{campaign_id}-batched-investigation-child-v1", 8102),
                        (f"{campaign_id}-batched-investigation-parent-v1", 8103),
                        (f"{campaign_id}-governed-remediation-v1", 8104),
                    )
                ),
                [],
            ),
        ]
    )

    app = compose_app(
        args,
        {
            "ZEROTH_EVALUATION_API_KEY": "service-secret-value",
            "ZEROTH_EVALUATION_FAULT_CONTROLLER_KEY": "controller-secret-value",
        },
    )

    response = TestClient(app).get(
        "/health", headers={"X-Controller-Key": "controller-secret-value"}
    )
    assert response.status_code == 200
    assert response.json() == {"campaign_id": campaign_id, "state": "ready"}
    assert (artifact_root / "fault-control.sqlite3").is_file()
    assert (artifact_root / "action-sink" / "actions.sqlite3").is_file()
