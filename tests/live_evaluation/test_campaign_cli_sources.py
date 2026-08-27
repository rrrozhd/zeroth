from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import release.live_evaluation.campaign_entrypoint as module
from release.live_evaluation.campaign_finalizer import EvidenceFirstCampaignFinalizer
from release.live_evaluation.cross_cutting_gates import EvidenceFirstCrossCuttingGateExecutor


def test_live_cli_wires_lazy_cross_cutting_sources_check_and_finalizer(
    tmp_path: Path, monkeypatch
) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    bundle = artifact_root / "bundle"
    bundle.mkdir()
    config = tmp_path / "campaign.json"
    config.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "campaign_id": "evaluation-studio-v1",
                "tenant_id": "evaluation-tenant",
                "provider": "openai",
                "model": "openai/gpt-4o-mini",
                "embedding_model": "openai/text-embedding-3-small",
                "vector_backend": "chroma",
                "campaign_budget_usd": "10.00",
                "per_run_cap_usd": "0.25",
                "provider_secret_ref": "llm.openai",
                "artifact_root": str(artifact_root),
                "action_sink_root": str(artifact_root / "sink"),
            }
        )
    )
    for name in ("discrepancies.md", "rollback.md", "project-model.md"):
        (tmp_path / name).write_text("# Safe handoff\n\nExecution rollback runtime risks reconciliation discrepancies.\n")
    snapshot = tmp_path / "reconciliation.json"
    snapshot.write_text("{}")
    captured = {}

    class Entrypoint:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run(self, *, dry_run: bool):
            return SimpleNamespace(
                campaign_id="evaluation-studio-v1",
                evidence_bundle=bundle,
                mode="live",
                summary=SimpleNamespace(completed=False),
            )

    monkeypatch.setattr(module, "CampaignEntrypoint", Entrypoint)
    monkeypatch.setattr(
        module,
        "load_live_execution_options",
        lambda environment, repository_root: SimpleNamespace(),
    )

    exit_code = module.main(
        [
            "--repository-root",
            str(tmp_path),
            "--campaign-config",
            str(config),
            "--evidence-bundle",
            str(bundle),
            "--console-url",
            "http://127.0.0.1:8100",
            "--fault-control-url",
            "http://127.0.0.1:8199",
            "--deployment-url",
            "evaluation-studio-v1-grounded-researcher-v1=http://127.0.0.1:8101",
            "--deployment-url",
            "evaluation-studio-v1-batched-investigation-child-v1=http://127.0.0.1:8102",
            "--deployment-url",
            "evaluation-studio-v1-batched-investigation-parent-v1=http://127.0.0.1:8103",
            "--deployment-url",
            "evaluation-studio-v1-governed-remediation-v1=http://127.0.0.1:8104",
            "--reconciliation-snapshot",
            str(snapshot),
            "--econ-db",
            str(tmp_path / "econ.sqlite3"),
            "--playwright-root",
            str(tmp_path / "browser"),
            "--no-produce-playwright",
            "--handoff-discrepancies",
            str(tmp_path / "discrepancies.md"),
            "--handoff-rollback",
            str(tmp_path / "rollback.md"),
            "--handoff-project-model",
            str(tmp_path / "project-model.md"),
            "--check-config",
            str(tmp_path / "zeroth-check.yaml"),
            "--execute",
        ]
    )

    assert exit_code == 2
    assert isinstance(captured["cross_cutting_gate_executor"], EvidenceFirstCrossCuttingGateExecutor)
    assert isinstance(captured["evidence_finalizer"], EvidenceFirstCampaignFinalizer)
    sources = captured["cross_cutting_gate_executor"].sources
    assert sources.playwright_root == (tmp_path / "browser")
    assert sources.reconciliation_collector is not None
    assert sources.reconciliation_collector.producer is not None
    command = sources.reconciliation_collector.producer.command
    bootstrap_index = command.index("evaluation-bootstrap=http://127.0.0.1:8100")
    assert command[bootstrap_index - 1] == "--deployment"
    provider_index = command.index("--provider-window")
    assert command[provider_index + 1].endswith(
        "evaluation-studio-v1.provider-window.json"
    )
