from __future__ import annotations

import importlib.util
import os
import plistlib
from pathlib import Path


SCRIPT = Path(__file__).parents[2] / "scripts" / "zeroth_dev_instance.py"


def _module():
    spec = importlib.util.spec_from_file_location("zeroth_dev_instance", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_init_preserves_stable_secrets_and_restricts_permissions(tmp_path: Path) -> None:
    dev = _module()
    state = tmp_path / "state"

    first = dev.initialize_state(state)
    second = dev.initialize_state(state)

    assert first == second
    assert set(first) == {
        "service-api-key",
        "tenant-a-operator-key",
        "tenant-a-reviewer-key",
        "tenant-a-admin-key",
        "tenant-b-operator-key",
        "tenant-b-reviewer-key",
        "tenant-b-admin-key",
        "tenant-b-platform-admin-key",
        "ecp-jwt-secret",
        "signing-key",
    }
    for path in first.values():
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.read_text().strip()


def test_launch_agents_are_keepalive_and_never_embed_secrets(tmp_path: Path) -> None:
    dev = _module()
    workspace = tmp_path / "zeroth"
    workspace.mkdir()
    state = tmp_path / "state"
    secret_paths = dev.initialize_state(state)
    market_env = tmp_path / "market.env"
    market_env.write_text("OPENAI_API_KEY=provider-secret\n")

    plists = dev.render_launch_agents(
        workspace=workspace,
        state_root=state,
        market_env=market_env,
        python_executable=Path("/usr/bin/python3"),
    )

    assert set(plists) == {"com.zeroth.dev.backend", "com.zeroth.dev.frontend"}
    serialized = b"".join(plistlib.dumps(value) for value in plists.values())
    assert b"provider-secret" not in serialized
    for path in secret_paths.values():
        assert path.read_bytes().strip() not in serialized
    for value in plists.values():
        assert value["KeepAlive"] is True
        assert value["RunAtLoad"] is True
        assert value["ProcessType"] == "Interactive"


def test_backend_environment_uses_persistent_paths_and_scoped_provider_key(
    tmp_path: Path,
) -> None:
    dev = _module()
    state = tmp_path / "state"
    dev.initialize_state(state)
    market_env = tmp_path / ".env"
    market_env.write_text("IGNORED=x\nOPENAI_API_KEY='provider-secret'\n")

    env = dev.backend_environment(state_root=state, market_env=market_env)

    assert env["ZEROTH_DATABASE__SQLITE_PATH"] == str(state / "zeroth.db")
    assert env["ECP_DATABASE_URL"].endswith("/econ.db")
    assert env["ZEROTH_SECRET__EVALUATION_STUDIO_V1__LLM_OPENAI"] == "provider-secret"
    assert env["ZEROTH_SECRETS__ALLOW_ENV_FALLBACK"] == "false"
    assert env["ZEROTH_REGULUS__PER_RUN_CAP_USD"] == "0.25"
    assert env["ZEROTH_REGULUS__FAIL_CLOSED"] == "true"
    assert "provider-secret" not in env["ZEROTH_SERVICE_API_KEYS_JSON"]
    assert os.path.isabs(env["ZEROTH_ARTIFACT_STORE__FILESYSTEM_BASE_DIR"])

    signing_key = (state / "runtime-secrets" / "signing-key").read_text().strip()
    retired_signing_keys = dev.json.loads(env["ZEROTH_PROVENANCE__RETIRED_KEYS_JSON"])
    assert retired_signing_keys == {
        "evaluation-local": signing_key,
        "evaluation-local-v1": signing_key,
    }

    credentials = dev.json.loads(env["ZEROTH_SERVICE_API_KEYS_JSON"])
    assert len(credentials) == 8
    assert {(credential["tenant_id"], credential["roles"][0]) for credential in credentials} == {
        (dev.PRIMARY_TENANT, "operator"),
        (dev.PRIMARY_TENANT, "reviewer"),
        (dev.PRIMARY_TENANT, "admin"),
        (dev.PRIMARY_TENANT, "platform_admin"),
        (dev.TWIN_TENANT, "operator"),
        (dev.TWIN_TENANT, "reviewer"),
        (dev.TWIN_TENANT, "admin"),
        (dev.TWIN_TENANT, "platform_admin"),
    }
    assert len({credential["secret"] for credential in credentials}) == 8
    assert all(credential["secret"] != "provider-secret" for credential in credentials)


def test_backend_environment_allows_provider_free_start_without_provider_key(
    tmp_path: Path,
) -> None:
    dev = _module()
    state = tmp_path / "state"
    market_env = tmp_path / ".env"
    market_env.write_text("ZEROTH_DEV_MARKER=provider-free\n")

    env = dev.backend_environment(state_root=state, market_env=market_env)

    assert "OPENAI_API_KEY" not in env
    assert "ZEROTH_SECRET__EVALUATION_STUDIO_V1__LLM_OPENAI" not in env
    assert "ZEROTH_SECRET__EVALUATION_STUDIO_V1_TWIN__LLM_OPENAI" not in env
    assert env["ZEROTH_SECRETS__ALLOW_ENV_FALLBACK"] == "false"
    assert env["ZEROTH_DATABASE__SQLITE_PATH"] == str(state / "zeroth.db")


def test_served_deployment_can_be_selected_without_changing_persistent_state() -> None:
    dev = _module()

    assert dev.selected_deployment_ref({}) == dev.DEPLOYMENT
    assert (
        dev.selected_deployment_ref({"ZEROTH_DEV_DEPLOYMENT_REF": "demo-loop-v1"}) == "demo-loop-v1"
    )
