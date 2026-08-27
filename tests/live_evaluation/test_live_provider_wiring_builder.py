from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from release.live_evaluation.live_provider_gate import _parse_wiring
from release.live_evaluation.live_provider_wiring_builder import build_wiring, main


CAMPAIGN = "evaluation-studio-v1"
FROZEN_D012 = {
    "status": "ok",
    "deployment_ref": "provider-free-child-approval-d012-20260826-2-parent",
    "deployment_version": 1,
    "graph_version_ref": "0179d403-2863-45f3-9556-58052a992da8@1",
}


def _write(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, bytes):
        path.write_bytes(value)
    else:
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    return path


def _fixture(tmp_path: Path) -> dict[str, Path]:
    state = tmp_path / "state"
    action_root = state / "action-sink"
    artifact_root = state
    campaign = _write(
        tmp_path / "campaign.json",
        {
            "schema_version": 1,
            "campaign_id": CAMPAIGN,
            "tenant_id": CAMPAIGN,
            "provider": "openai",
            "model": "openai/gpt-4o-mini",
            "embedding_model": "openai/text-embedding-3-small",
            "vector_backend": "chroma",
            "campaign_budget_usd": "10.00",
            "per_run_cap_usd": "0.25",
            "provider_secret_ref": "llm.openai",
            "artifact_root": str(artifact_root),
            "action_sink_root": str(action_root),
        },
    )
    service = _write(state / "zeroth.db", b"service-plane-placeholder")
    economics = _write(state / "econ.db", b"economics-plane-placeholder")
    action = _write(action_root / "actions.sqlite3", b"action-plane-placeholder")
    provider_window = _write(
        state / "reconciliation" / f"{CAMPAIGN}.provider-window.json",
        {"window_id": "shared-project-window-1", "total_usd": "0.01"},
    )
    template = _write(
        tmp_path / "template-fixture.json",
        {
            "status": "provisioned",
            "config": {
                "fixture_id": "live-template-render-20260826",
                "tenant_id": CAMPAIGN,
                "template_name": "live-template-render-20260826",
                "deployment_ref": "live-template-render-20260826-v1",
            },
            "pre_health": FROZEN_D012,
            "post_health": FROZEN_D012,
            "fixture": {
                "fixture_id": "live-template-render-20260826",
                "template_name": "live-template-render-20260826",
                "template_version": 1,
                "workflow_id": "workflow-template-live",
                "graph_version_ref": "workflow-template-live@1",
                "deployment_ref": "live-template-render-20260826-v1",
                "deployment_version": 1,
                "provider_calls_performed": 0,
            },
            "provider_calls_performed": 0,
        },
    )
    batch = _write(
        tmp_path / "batch.json",
        {
            "schema_version": 1,
            "items": [
                {"index": index, "query": f"Investigate realistic incident scenario {index}."}
                for index in range(8)
            ],
        },
    )
    cases = _write(
        tmp_path / "cases.json",
        {
            "schema_version": 1,
            "cases": [
                {
                    "case_id": "incident-priority",
                    "input": {"request": "Classify a production outage."},
                    "reference": "P1 because production is unavailable.",
                },
                {
                    "case_id": "deployment-safety",
                    "input": {"request": "A reviewed checksum differs."},
                    "reference": "Stop and reconcile the artifact.",
                },
            ],
        },
    )
    return {
        "campaign": campaign,
        "service": service,
        "economics": economics,
        "action": action,
        "provider_window": provider_window,
        "template": template,
        "batch": batch,
        "cases": cases,
        "output": tmp_path / "wiring.json",
    }


def _build(paths: dict[str, Path]) -> dict[str, object]:
    return build_wiring(
        campaign_config=paths["campaign"],
        service_database=paths["service"],
        economics_database=paths["economics"],
        action_sink_database=paths["action"],
        provider_window=paths["provider_window"],
        service_base_url="http://127.0.0.1:8122",
        template_fixture=paths["template"],
        batch_fixture=paths["batch"],
        rightsizing_cases=paths["cases"],
    )


def test_builds_exact_gate_wiring_from_real_paths_without_runtime_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _fixture(tmp_path)
    monkeypatch.setattr(
        sqlite3,
        "connect",
        lambda *_args, **_kwargs: pytest.fail("builder must not open a database"),
    )
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: pytest.fail("builder must not perform HTTP"),
    )

    wiring = _build(paths)

    assert set(wiring) == {
        "schema_version",
        "service_base_url",
        "service_database",
        "econ_database",
        "action_sink_database",
        "provider_window",
        "batch_items",
        "template",
        "rightsizing",
    }
    assert wiring["service_base_url"] == "http://127.0.0.1:8122"
    assert [item["index"] for item in wiring["batch_items"]] == list(range(8))
    assert wiring["template"]["tenant_id"] == CAMPAIGN
    assert wiring["rightsizing"] == {
        "cases_sha256": hashlib.sha256(paths["cases"].read_bytes()).hexdigest(),
        "node_id": "research-agent",
        "incumbent": "openai/gpt-4o-mini",
        "instruction": "Answer only from supplied context.",
        "needs_tools": False,
        "needs_vision": False,
        "judge_model": "openai/gpt-4o-mini",
        "max_candidates": 1,
        "max_cases": 2,
        "min_cases": 2,
        "tolerance_pct": 5.0,
        "mode": "equivalence",
    }
    parsed = _parse_wiring(wiring)
    assert parsed.rightsizing_cases_sha256 == wiring["rightsizing"]["cases_sha256"]
    serialized = json.dumps(wiring, sort_keys=True)
    assert "provider_calls_performed" in serialized
    assert "api_key" not in serialized.lower()
    assert "authorization" not in serialized.lower()


@pytest.mark.parametrize(
    ("budget_field", "value"),
    (("campaign_budget_usd", "9.99"), ("per_run_cap_usd", "0.20")),
)
def test_requires_exact_existing_campaign_budget_contract(
    tmp_path: Path, budget_field: str, value: str
) -> None:
    paths = _fixture(tmp_path)
    campaign = json.loads(paths["campaign"].read_text(encoding="utf-8"))
    campaign[budget_field] = value
    paths["campaign"].write_text(json.dumps(campaign), encoding="utf-8")

    with pytest.raises(ValueError, match="exactly"):
        _build(paths)


def test_rejects_flat_template_fixture_even_when_fixture_fields_are_valid(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    wrapped = json.loads(paths["template"].read_text(encoding="utf-8"))
    paths["template"].write_text(json.dumps(wrapped["fixture"]), encoding="utf-8")

    with pytest.raises(ValueError, match="provisioning result"):
        _build(paths)


@pytest.mark.parametrize(
    "mutation",
    (
        "status",
        "outer-provider-call",
        "fixture-provider-call",
        "tenant",
        "config-identity",
        "pre-health",
        "post-health",
        "outer-extra",
        "config-extra",
        "health-extra",
        "fixture-extra",
    ),
)
def test_rejects_any_template_provisioning_result_drift(tmp_path: Path, mutation: str) -> None:
    paths = _fixture(tmp_path)
    wrapped = json.loads(paths["template"].read_text(encoding="utf-8"))
    if mutation == "status":
        wrapped["status"] = "blocked"
    elif mutation == "outer-provider-call":
        wrapped["provider_calls_performed"] = 1
    elif mutation == "fixture-provider-call":
        wrapped["fixture"]["provider_calls_performed"] = 1
    elif mutation == "tenant":
        wrapped["config"]["tenant_id"] = "evaluation-other"
    elif mutation == "config-identity":
        wrapped["config"]["deployment_ref"] = "other-v1"
    elif mutation == "pre-health":
        wrapped["pre_health"]["deployment_version"] = 2
    elif mutation == "post-health":
        wrapped["post_health"]["graph_version_ref"] = "other@1"
    elif mutation == "outer-extra":
        wrapped["extra"] = True
    elif mutation == "config-extra":
        wrapped["config"]["extra"] = True
    elif mutation == "health-extra":
        wrapped["pre_health"]["extra"] = True
    else:
        wrapped["fixture"]["extra"] = True
    paths["template"].write_text(json.dumps(wrapped), encoding="utf-8")

    with pytest.raises(ValueError, match="provisioning result"):
        _build(paths)


@pytest.mark.parametrize("mutation", ("unknown-document-key", "unknown-item-key", "blank", "index"))
def test_rejects_any_batch_contract_drift(tmp_path: Path, mutation: str) -> None:
    paths = _fixture(tmp_path)
    batch = json.loads(paths["batch"].read_text(encoding="utf-8"))
    if mutation == "unknown-document-key":
        batch["extra"] = True
    elif mutation == "unknown-item-key":
        batch["items"][0]["extra"] = True
    elif mutation == "blank":
        batch["items"][0]["query"] = "   "
    else:
        batch["items"][3]["index"] = 4
    paths["batch"].write_text(json.dumps(batch), encoding="utf-8")

    with pytest.raises(ValueError, match="batch fixture"):
        _build(paths)


@pytest.mark.parametrize("source", ("template", "batch", "cases", "provider_window"))
def test_rejects_secret_shaped_json_content(tmp_path: Path, source: str) -> None:
    paths = _fixture(tmp_path)
    value = json.loads(paths[source].read_text(encoding="utf-8"))
    value["authorization"] = "Bearer sk-proj-secret-material-1234567890"
    paths[source].write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ValueError, match="secret-shaped"):
        _build(paths)


def test_refuses_missing_outside_or_symlinked_persistent_paths(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    paths["service"] = tmp_path / "missing.db"
    with pytest.raises(ValueError, match="persistent path"):
        _build(paths)

    paths = _fixture(tmp_path / "outside")
    outside = _write(tmp_path / "outside-econ.db", b"outside")
    paths["economics"] = outside
    with pytest.raises(ValueError, match="persistent path"):
        _build(paths)

    paths = _fixture(tmp_path / "symlink")
    link = paths["service"].with_name("service-link.db")
    link.symlink_to(paths["service"])
    paths["service"] = link
    with pytest.raises(ValueError, match="persistent path"):
        _build(paths)


def test_cli_writes_create_only_json_and_does_not_expose_input_content(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = _fixture(tmp_path)
    argv = [
        "--campaign-config",
        str(paths["campaign"]),
        "--service-db",
        str(paths["service"]),
        "--econ-db",
        str(paths["economics"]),
        "--action-sink-db",
        str(paths["action"]),
        "--provider-window",
        str(paths["provider_window"]),
        "--service-base-url",
        "http://127.0.0.1:8122",
        "--template-fixture",
        str(paths["template"]),
        "--batch-fixture",
        str(paths["batch"]),
        "--rightsizing-cases",
        str(paths["cases"]),
        "--output",
        str(paths["output"]),
    ]

    assert main(argv) == 0
    assert _parse_wiring(json.loads(paths["output"].read_text(encoding="utf-8")))
    output = json.loads(capsys.readouterr().out)
    assert output == {"created": str(paths["output"]), "provider_calls_performed": 0}

    with pytest.raises(FileExistsError):
        main(argv)
    assert "secret" not in capsys.readouterr().out.lower()
