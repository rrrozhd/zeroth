from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import yaml

from release.live_evaluation import persistent_dev_readiness
from release.live_evaluation.persistent_dev_readiness import (
    EndpointObservation,
    discover_persistent_dev_runtime,
)


def _fixture(tmp_path: Path, *, provider_window: bool = True):
    repository = tmp_path / "repository"
    repository.mkdir()
    state = tmp_path / "state"
    (state / "action-sink").mkdir(parents=True)
    (state / "runtime-secrets").mkdir()
    (state / "reconciliation").mkdir()
    for name, content in (
        ("service-api-key", "service-secret-must-not-escape"),
        ("fault-controller-key", "controller-secret-must-not-escape"),
        ("signing-key", "signing-secret-must-not-escape"),
    ):
        path = state / "runtime-secrets" / name
        path.write_text(content)
        path.chmod(0o600)
    market = repository / ".dev-secrets" / "zeroth.env"
    market.parent.mkdir()
    market.write_text("OPENAI_API_KEY=provider-secret-must-not-escape\n")
    market.chmod(0o600)
    campaign = repository / "campaign.json"
    campaign.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "campaign_id": "evaluation-campaign-a",
                "tenant_id": "evaluation-tenant-a",
                "provider": "openai",
                "model": "openai/gpt-4o-mini",
                "embedding_model": "openai/text-embedding-3-small",
                "vector_backend": "chroma",
                "campaign_budget_usd": "10.00",
                "per_run_cap_usd": "0.25",
                "provider_secret_ref": "llm.openai",
                "artifact_root": str(state),
                "action_sink_root": str(state / "action-sink"),
            }
        )
    )
    compose = repository / "compose.dev.yml"
    compose.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "backend": {"ports": ["127.0.0.1:8122:8122"]},
                    "backend-twin": {"ports": ["127.0.0.1:8123:8123"]},
                    "frontend": {"ports": ["127.0.0.1:3000:3000"]},
                    "chroma": {"ports": ["127.0.0.1:8121:8000"]},
                }
            }
        )
    )
    with sqlite3.connect(state / "zeroth.db") as database:
        database.execute(
            "CREATE TABLE runs (run_id TEXT, tenant_id TEXT, metadata TEXT, started_at TEXT)"
        )
        database.execute(
            "INSERT INTO runs VALUES (?,?,?,?)",
            (
                "run-1",
                "evaluation-tenant-a",
                json.dumps({"campaign_id": "evaluation-campaign-a"}),
                "1",
            ),
        )
    with sqlite3.connect(state / "econ.db") as database:
        database.execute("CREATE TABLE execution_events (execution_id TEXT)")
    with sqlite3.connect(state / "action-sink" / "actions.sqlite3") as database:
        database.execute("CREATE TABLE action_markers (operation_key TEXT)")
    if provider_window:
        (state / "reconciliation" / "evaluation-campaign-a.provider-window.json").write_text(
            json.dumps({"window_id": "window-1", "total_usd": "0.01"})
        )
    return repository, state, campaign, compose, market


def _observations(active_deployment: str) -> dict[str, EndpointObservation]:
    return {
        "http://127.0.0.1:8122/health": EndpointObservation(
            status_code=200,
            payload={
                "status": "ok",
                "campaign_id": "evaluation-campaign-a",
                "deployment_ref": active_deployment,
            },
        ),
        "http://127.0.0.1:8123/health": EndpointObservation(
            status_code=200,
            payload={"status": "ok", "deployment_ref": "twin-bootstrap"},
        ),
        "http://127.0.0.1:8121/api/v2/heartbeat": EndpointObservation(
            status_code=200, payload={"nanosecond heartbeat": 1}
        ),
        "http://127.0.0.1:3000/console/": EndpointObservation(status_code=200, payload=None),
    }


def test_discovers_exact_provider_free_wiring_without_secret_values(tmp_path: Path) -> None:
    repository, state, campaign, compose, market = _fixture(tmp_path)
    initial = discover_persistent_dev_runtime(
        repository_root=repository,
        state_root=state,
        campaign_config=campaign,
        compose_file=compose,
        market_env=market,
        endpoint_observations={},
    )
    active = initial.campaign_deployment_refs[0]

    result = discover_persistent_dev_runtime(
        repository_root=repository,
        state_root=state,
        campaign_config=campaign,
        compose_file=compose,
        market_env=market,
        endpoint_observations=_observations(active),
    )

    assert result.ready is True
    assert result.service_database == state / "zeroth.db"
    assert result.econ_database == state / "econ.db"
    assert result.action_sink_database == state / "action-sink" / "actions.sqlite3"
    assert result.provider_window == (
        state / "reconciliation/evaluation-campaign-a.provider-window.json"
    )
    assert result.campaign_run_ids == ("run-1",)
    assert set(result.audit_endpoints) == set(result.campaign_deployment_refs)
    assert set(result.audit_endpoints.values()) == {
        f"http://127.0.0.1:8122/v1/deployments/{reference}/audits"
        for reference in result.campaign_deployment_refs
    }
    assert result.backend_restart[-2:] == ("restart", "backend")
    assert result.frontend_restart[-2:] == ("restart", "frontend")
    serialized = repr(result)
    for secret in (
        "service-secret-must-not-escape",
        "controller-secret-must-not-escape",
        "signing-secret-must-not-escape",
        "provider-secret-must-not-escape",
    ):
        assert secret not in serialized
    assert {reference.name for reference in result.secret_references} == {
        "provider_catalog",
        "provider_environment_file",
        "service_api_key",
        "fault_controller_key",
        "signing_key",
    }


def test_missing_provider_window_and_wrong_active_deployment_are_blockers(tmp_path: Path) -> None:
    repository, state, campaign, compose, market = _fixture(tmp_path, provider_window=False)

    result = discover_persistent_dev_runtime(
        repository_root=repository,
        state_root=state,
        campaign_config=campaign,
        compose_file=compose,
        market_env=market,
        endpoint_observations=_observations("unrelated-deployment"),
    )

    assert result.ready is False
    assert set(result.blockers) == {
        "active_deployment_not_campaign_topology",
        "provider_window_missing",
    }


def test_missing_twin_and_campaign_inventory_fail_closed(tmp_path: Path) -> None:
    repository, state, campaign, compose, market = _fixture(tmp_path)
    with sqlite3.connect(state / "zeroth.db") as database:
        database.execute("DELETE FROM runs")
    observations = _observations("ignored")
    first = discover_persistent_dev_runtime(
        repository_root=repository,
        state_root=state,
        campaign_config=campaign,
        compose_file=compose,
        market_env=market,
        endpoint_observations={},
    )
    observations["http://127.0.0.1:8122/health"] = EndpointObservation(
        status_code=200,
        payload={
            "status": "ok",
            "campaign_id": "evaluation-campaign-a",
            "deployment_ref": first.campaign_deployment_refs[0],
        },
    )
    observations["http://127.0.0.1:8123/health"] = EndpointObservation(status_code=0, payload=None)

    result = discover_persistent_dev_runtime(
        repository_root=repository,
        state_root=state,
        campaign_config=campaign,
        compose_file=compose,
        market_env=market,
        endpoint_observations=observations,
    )

    assert "campaign_run_inventory_empty" in result.blockers
    assert "twin_backend_unavailable" in result.blockers


def test_injected_observations_never_open_a_network_connection(tmp_path: Path, monkeypatch) -> None:
    repository, state, campaign, compose, market = _fixture(tmp_path)
    initial = discover_persistent_dev_runtime(
        repository_root=repository,
        state_root=state,
        campaign_config=campaign,
        compose_file=compose,
        market_env=market,
        endpoint_observations={},
    )

    def reject_probe(_url: str) -> EndpointObservation:
        raise AssertionError("network probe must not run with injected observations")

    monkeypatch.setattr(persistent_dev_readiness, "_probe", reject_probe)
    result = discover_persistent_dev_runtime(
        repository_root=repository,
        state_root=state,
        campaign_config=campaign,
        compose_file=compose,
        market_env=market,
        endpoint_observations=_observations(initial.campaign_deployment_refs[0]),
    )

    assert result.ready is True


def test_empty_secret_reference_fails_closed_without_reading_it(tmp_path: Path) -> None:
    repository, state, campaign, compose, market = _fixture(tmp_path)
    market.write_bytes(b"")
    initial = discover_persistent_dev_runtime(
        repository_root=repository,
        state_root=state,
        campaign_config=campaign,
        compose_file=compose,
        market_env=market,
        endpoint_observations={},
    )

    result = discover_persistent_dev_runtime(
        repository_root=repository,
        state_root=state,
        campaign_config=campaign,
        compose_file=compose,
        market_env=market,
        endpoint_observations=_observations(initial.campaign_deployment_refs[0]),
    )

    assert "required_secret_reference_unavailable" in result.blockers
