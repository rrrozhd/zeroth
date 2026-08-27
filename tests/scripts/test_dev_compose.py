from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).parents[2]


def _compose() -> dict:
    return yaml.safe_load((ROOT / "compose.dev.yml").read_text())


def _retention_compose() -> dict:
    return yaml.safe_load((ROOT / "compose.retention-evidence.yml").read_text())


def test_dev_services_keep_stable_ports_state_and_restart_policy() -> None:
    compose = _compose()
    services = compose["services"]

    assert set(services) == {"backend", "backend-twin", "frontend", "chroma", "redis"}
    assert services["backend"]["ports"] == ["127.0.0.1:8122:8122"]
    assert services["backend-twin"]["ports"] == ["127.0.0.1:8123:8123"]
    assert services["frontend"]["ports"] == ["127.0.0.1:3000:3000"]
    assert services["chroma"]["ports"] == ["127.0.0.1:8121:8000"]
    assert all(service["restart"] == "unless-stopped" for service in services.values())
    assert any("/state" in volume for volume in services["backend"]["volumes"])
    assert any("/state" in volume for volume in services["backend-twin"]["volumes"])
    assert any("chroma-data" in volume for volume in services["chroma"]["volumes"])


def test_backends_use_the_persistent_healthy_redis_service() -> None:
    compose = _compose()
    services = compose["services"]
    redis = services["redis"]

    assert redis["image"] == "redis:7.4.2-alpine"
    assert redis["command"] == ["redis-server", "--appendonly", "yes"]
    assert redis["volumes"] == ["zeroth-redis:/data"]
    assert redis["healthcheck"]["test"] == ["CMD", "redis-cli", "ping"]
    for name in ("backend", "backend-twin"):
        assert services[name]["environment"]["ZEROTH_REDIS__HOST"] == "redis"
        assert services[name]["depends_on"]["redis"]["condition"] == "service_healthy"


def test_chroma_uses_actual_data_path_and_an_available_bash_tcp_healthcheck() -> None:
    chroma = _compose()["services"]["chroma"]
    assert any(volume.endswith(":/data") for volume in chroma["volumes"])
    assert chroma["healthcheck"]["test"][:3] == ["CMD", "/usr/bin/bash", "-c"]
    assert "/dev/tcp/127.0.0.1/8000" in chroma["healthcheck"]["test"][3]
    assert "wget" not in " ".join(chroma["healthcheck"]["test"])


def test_dev_compose_never_embeds_provider_or_service_credentials() -> None:
    source = (ROOT / "compose.dev.yml").read_text()
    assert "sk-" not in source
    assert "ZEROTH_SERVICE_API_KEYS_JSON" not in source
    backend = _compose()["services"]["backend"]
    assert backend["env_file"] == [
        {"path": "${ZEROTH_DEV_ENV_FILE:-.dev-secrets/zeroth.env}", "required": True}
    ]
    assert (
        "${ZEROTH_DEV_ENV_FILE:-.dev-secrets/zeroth.env}:/run/secrets/zeroth.env:ro"
        in backend["volumes"]
    )


def test_backend_and_frontend_are_reloadable_from_bind_mounted_source() -> None:
    services = _compose()["services"]
    backend_volumes = services["backend"]["volumes"]
    frontend_volumes = services["frontend"]["volumes"]
    assert ".:/workspace" in backend_volumes
    assert "./frontend:/workspace/frontend" in frontend_volumes
    assert "zeroth-frontend-modules:/workspace/frontend/node_modules" in frontend_volumes
    assert "zeroth-frontend-next:/workspace/frontend/.next" in frontend_volumes


def test_frontend_uses_stable_webpack_dev_server() -> None:
    command = _compose()["services"]["frontend"]["command"]
    assert "--webpack" in command[-1]
    assert "--turbopack" not in command[-1]


def test_backend_image_installs_every_runtime_extra_imported_at_boot() -> None:
    dockerfile = (ROOT / "Dockerfile.dev").read_text()
    for extra in ("langgraph-gateway", "memory-chroma", "regulus"):
        assert f"--extra {extra}" in dockerfile


def test_backend_binds_container_interface_while_host_publish_stays_loopback_only() -> None:
    backend = _compose()["services"]["backend"]
    assert backend["environment"]["ZEROTH_DEV_BIND_HOST"] == "0.0.0.0"
    assert backend["ports"] == ["127.0.0.1:8122:8122"]
    twin = _compose()["services"]["backend-twin"]
    assert twin["environment"]["ZEROTH_DEV_BIND_HOST"] == "0.0.0.0"
    assert twin["environment"]["ZEROTH_DEV_TENANT_ID"] == "evaluation-studio-v1-twin"
    assert twin["environment"]["ZEROTH_DEV_PORT"] == "8123"
    assert twin["ports"] == ["127.0.0.1:8123:8123"]
    launcher = (ROOT / "scripts/zeroth_dev_instance.py").read_text()
    assert 'os.environ.get("ZEROTH_DEV_BIND_HOST", "127.0.0.1")' in launcher


def test_primary_dev_service_restarts_on_the_validated_workflow3_deployment() -> None:
    deployment = _compose()["services"]["backend"]["environment"][
        "ZEROTH_DEV_DEPLOYMENT_REF"
    ]
    assert deployment == (
        "${ZEROTH_DEV_DEPLOYMENT_REF:-"
        "evaluation-studio-v1-governed-remediation-v2}"
    )


def test_retention_evidence_service_isolated_persistent_and_provider_free() -> None:
    service = _retention_compose()["services"]["backend-retention-evidence"]

    assert service["restart"] == "unless-stopped"
    assert service["ports"] == ["127.0.0.1:8124:8124"]
    assert service["environment"]["ZEROTH_DEV_TENANT_ID"] == "evaluation-studio-v1-twin"
    assert service["environment"]["ZEROTH_DEV_DEPLOYMENT_REF"] == "demo-artifact-output-v1"
    assert service["environment"]["ZEROTH_DEV_SEED_ARTIFACT_DEMO"] == "1"
    assert any(
        "evaluation-studio-v1-retention-disposable:/state" in volume
        for volume in service["volumes"]
    )
    launcher = (ROOT / "scripts/zeroth_dev_instance.py").read_text()
    assert 'arguments.append("--seed-artifact-demo")' in launcher
    source = (ROOT / "compose.retention-evidence.yml").read_text()
    assert "sk-" not in source
    assert "OPENAI_API_KEY" not in source
