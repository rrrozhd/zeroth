from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from release.live_evaluation.live_provider_gate import ARM_ENVIRONMENT_VARIABLE, main


CAMPAIGN = "evaluation-studio-v1"


def _files(tmp_path: Path, *, tenant_id: str = CAMPAIGN) -> tuple[Path, Path, Path]:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    campaign = tmp_path / "campaign.json"
    campaign.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "campaign_id": CAMPAIGN,
                "tenant_id": tenant_id,
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
        )
    )
    readiness = tmp_path / "readiness.json"
    readiness.write_text(
        json.dumps(
            {
                "campaign_id": CAMPAIGN,
                "tenant_id": tenant_id,
                "logical_secret_ref": "llm.openai",
                "installed": True,
                "provider_probe_reconciled": True,
                "provider_request_id": "readiness-provider",
                "operation_id": "readiness-operation",
                "run_id": "readiness-run",
                "audit_event_id": "readiness-audit",
                "cost_event_id": "readiness-cost",
                "measured_cost_usd": "0.000001",
                "campaign_spend_before_usd": "0.000001",
                "audit_signed": True,
            }
        )
    )
    paths = {}
    for name in (
        "service_database",
        "econ_database",
        "action_sink_database",
        "provider_window",
    ):
        path = tmp_path / f"{name}.fixture"
        path.write_bytes(b"provider-free-placeholder")
        paths[name] = str(path)
    wiring = tmp_path / "wiring.json"
    wiring.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "service_base_url": "http://127.0.0.1:8122",
                **paths,
                "batch_items": [{"index": index, "query": f"item-{index}"} for index in range(8)],
                "template": {
                    "fixture_id": "template-render-20260826",
                    "tenant_id": tenant_id,
                    "template_name": "live-render-template-20260826",
                    "deployment_ref": "live-render-template-20260826-v1",
                    "template_version": 1,
                    "workflow_id": "workflow-template-live",
                    "graph_version_ref": "workflow-template-live@1",
                    "deployment_version": 1,
                    "provider_calls_performed": 0,
                },
                "rightsizing": {
                    "cases_sha256": "a" * 64,
                    "node_id": "research-agent",
                    "incumbent": "openai/gpt-4o-mini",
                    "instruction": "Answer only from supplied context.",
                    "needs_tools": False,
                    "needs_vision": False,
                    "judge_model": "openai/gpt-4o-mini",
                    "max_candidates": 1,
                    "max_cases": 1,
                    "min_cases": 1,
                    "tolerance_pct": 5.0,
                    "mode": "equivalence",
                },
            }
        )
    )
    return campaign, readiness, wiring


def _argv(paths: tuple[Path, Path, Path], *, arm: bool = False) -> list[str]:
    campaign, readiness, wiring = paths
    values = [
        "readiness",
        "--campaign-config",
        str(campaign),
        "--readiness-attestation",
        str(readiness),
        "--wiring-config",
        str(wiring),
    ]
    if arm:
        values.append("--arm-live-provider")
    return values


def test_readiness_is_provider_free_and_reports_exact_order(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    paths = _files(tmp_path)
    monkeypatch.delenv(ARM_ENVIRONMENT_VARIABLE, raising=False)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: pytest.fail("readiness must not perform HTTP"),
    )
    monkeypatch.setattr(
        sqlite3,
        "connect",
        lambda *_args, **_kwargs: pytest.fail("readiness must not open databases"),
    )

    result = main(_argv(paths))

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload == {
        "armed": False,
        "blockers": [
            "explicit --arm-live-provider flag is absent",
            f"{ARM_ENVIRONMENT_VARIABLE} does not equal {CAMPAIGN}",
        ],
        "campaign_id": CAMPAIGN,
        "configuration_ready": True,
        "execution_ready": False,
        "planned_order": [
            "batching.provider-economics",
            "templates.live-rendered-execution",
            "rightsizing.measured-experiment",
            "rightsizing.cost-reconciliation",
        ],
        "provider_calls_performed": 0,
        "tenant_id": CAMPAIGN,
    }


def test_both_operator_interlocks_make_valid_configuration_execution_ready(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    paths = _files(tmp_path)
    monkeypatch.setenv(ARM_ENVIRONMENT_VARIABLE, CAMPAIGN)

    result = main(_argv(paths, arm=True))

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["configuration_ready"] is True
    assert payload["armed"] is True
    assert payload["execution_ready"] is True
    assert payload["blockers"] == []
    assert payload["provider_calls_performed"] == 0


def test_tenant_campaign_mismatch_fails_closed_before_runtime_access(
    tmp_path: Path, capsys
) -> None:
    paths = _files(tmp_path, tenant_id="evaluation-other-tenant")

    result = main(_argv(paths))

    payload = json.loads(capsys.readouterr().out)
    assert result == 2
    assert payload == {
        "configuration_ready": False,
        "provider_calls_performed": 0,
        "reason": "template_campaign_tenant_identity_mismatch",
    }


def test_cli_has_no_argument_that_can_receive_a_credential_value(tmp_path: Path, capsys) -> None:
    paths = _files(tmp_path)

    with pytest.raises(SystemExit):
        main([*_argv(paths), "--api-key", "service-secret-must-not-be-accepted"])

    output = capsys.readouterr()
    assert "service-secret-must-not-be-accepted" not in output.out + output.err
