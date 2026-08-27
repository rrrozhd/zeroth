#!/usr/bin/env python3
"""Persistent local Zeroth development instance for the evaluation deployment."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import plistlib
import secrets
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

PRIMARY_TENANT = "evaluation-studio-v1"
TWIN_TENANT = "evaluation-studio-v1-twin"
TENANT = PRIMARY_TENANT
DEPLOYMENT = "evaluation-studio-v1-grounded-researcher-v1"
BACKEND_LABEL = "com.zeroth.dev.backend"
FRONTEND_LABEL = "com.zeroth.dev.frontend"
ROLE_SECRET_NAMES = {
    (PRIMARY_TENANT, "platform_admin"): "service-api-key",
    (PRIMARY_TENANT, "operator"): "tenant-a-operator-key",
    (PRIMARY_TENANT, "reviewer"): "tenant-a-reviewer-key",
    (PRIMARY_TENANT, "admin"): "tenant-a-admin-key",
    (TWIN_TENANT, "operator"): "tenant-b-operator-key",
    (TWIN_TENANT, "reviewer"): "tenant-b-reviewer-key",
    (TWIN_TENANT, "admin"): "tenant-b-admin-key",
    (TWIN_TENANT, "platform_admin"): "tenant-b-platform-admin-key",
}
SECRET_NAMES = (*ROLE_SECRET_NAMES.values(), "ecp-jwt-secret", "signing-key")


def selected_deployment_ref(environment: dict[str, str] | os._Environ[str]) -> str:
    """Resolve the deployment served by this process without touching state."""
    selected = environment.get("ZEROTH_DEV_DEPLOYMENT_REF", DEPLOYMENT).strip()
    if not selected:
        raise RuntimeError("ZEROTH_DEV_DEPLOYMENT_REF must be non-empty")
    return selected


def selected_tenant_id(environment: dict[str, str] | os._Environ[str]) -> str:
    """Resolve the tenant served by this process without touching state."""
    selected = environment.get("ZEROTH_DEV_TENANT_ID", PRIMARY_TENANT).strip()
    if selected not in {PRIMARY_TENANT, TWIN_TENANT}:
        raise RuntimeError("ZEROTH_DEV_TENANT_ID must select a campaign fixture tenant")
    return selected


def _service_credentials(secret_paths: dict[str, Path]) -> list[dict[str, object]]:
    credentials: list[dict[str, object]] = []
    for (tenant_id, role), secret_name in ROLE_SECRET_NAMES.items():
        fixture = "a" if tenant_id == PRIMARY_TENANT else "b"
        credentials.append(
            {
                "credential_id": f"evaluation-{fixture}-{role.replace('_', '-')}",
                "secret": secret_paths[secret_name].read_text().strip(),
                "subject": f"evaluation-{fixture}-{role.replace('_', '-')}",
                "roles": [role],
                "tenant_id": tenant_id,
            }
        )
    return credentials


def _write_private_once(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        os.chmod(path, 0o600)
        return
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(secrets.token_hex(32) + "\n")


def initialize_state(state_root: Path) -> dict[str, Path]:
    """Create stable dev credentials exactly once and return their paths."""
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state_root, 0o700)
    secret_root = state_root / "runtime-secrets"
    paths = {name: secret_root / name for name in SECRET_NAMES}
    for path in paths.values():
        _write_private_once(path)
    (state_root / "logs").mkdir(exist_ok=True, mode=0o700)
    return paths


def _read_env_value(path: Path, name: str, *, required: bool = True) -> str | None:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        if key.removeprefix("export ").strip() != name:
            continue
        values = shlex.split(raw_value, posix=True)
        if len(values) != 1 or not values[0]:
            raise RuntimeError(f"{name} in {path} is not a single non-empty value")
        return values[0]
    if required:
        raise RuntimeError(f"{name} is missing from {path}")
    return None


def backend_environment(
    *, state_root: Path, market_env: Path, tenant_id: str = PRIMARY_TENANT
) -> dict[str, str]:
    """Build the private service environment; callers must never log the result."""
    secret_paths = initialize_state(state_root)
    provider_key = _read_env_value(market_env, "OPENAI_API_KEY", required=False)
    signing_key = secret_paths["signing-key"].read_text().strip()
    ecp_key = secret_paths["ecp-jwt-secret"].read_text().strip()
    service_credentials = json.dumps(_service_credentials(secret_paths), separators=(",", ":"))
    retired_signing_keys = json.dumps(
        {
            "evaluation-local": signing_key,
            "evaluation-local-v1": signing_key,
        },
        separators=(",", ":"),
    )
    environment = {
        "ZEROTH_DATABASE__BACKEND": "sqlite",
        "ZEROTH_DATABASE__SQLITE_PATH": str(state_root / "zeroth.db"),
        "ECP_DATABASE_URL": f"sqlite+pysqlite:////{state_root / 'econ.db'}",
        "ECP_JWT_SECRET": ecp_key,
        "ECP_SERVICE_PRINCIPAL_TENANT_ID": tenant_id,
        "ZEROTH_SECRETS__BACKEND": "env",
        "ZEROTH_SECRETS__ALLOW_ENV_FALLBACK": "false",
        "SIGNING_DEPLOYMENT": signing_key,
        "ZEROTH_PROVENANCE__SIGNING_KEY_ID": "dev-local",
        "ZEROTH_PROVENANCE__RETIRED_KEYS_JSON": retired_signing_keys,
        "ZEROTH_SERVICE_API_KEYS_JSON": service_credentials,
        "ZEROTH_REGULUS__FAIL_CLOSED": "true",
        "ZEROTH_REGULUS__PER_RUN_CAP_USD": "0.25",
        "ZEROTH_CONSOLE_CORS_ORIGINS": (
            "http://127.0.0.1:3000,http://localhost:3000,http://127.0.0.1:8122"
        ),
        "ZEROTH_ARTIFACT_STORE__FILESYSTEM_BASE_DIR": str(state_root / "runtime-artifacts"),
    }
    if provider_key is not None:
        environment.update(
            {
                "OPENAI_API_KEY": provider_key,
                "ZEROTH_SECRET__EVALUATION_STUDIO_V1__LLM_OPENAI": provider_key,
                "ZEROTH_SECRET__EVALUATION_STUDIO_V1_TWIN__LLM_OPENAI": provider_key,
            }
        )
    return environment


def _agent(
    *,
    label: str,
    arguments: list[str],
    workspace: Path,
    state_root: Path,
) -> dict[str, Any]:
    log_stem = label.rsplit(".", 1)[-1]
    return {
        "Label": label,
        "ProgramArguments": arguments,
        "WorkingDirectory": str(workspace),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Interactive",
        "ThrottleInterval": 5,
        "StandardOutPath": str(state_root / "logs" / f"{log_stem}.log"),
        "StandardErrorPath": str(state_root / "logs" / f"{log_stem}.error.log"),
    }


def render_launch_agents(
    *,
    workspace: Path,
    state_root: Path,
    market_env: Path,
    python_executable: Path,
) -> dict[str, dict[str, Any]]:
    """Render launchd definitions containing paths only, never credentials."""
    script = Path(__file__).resolve()
    common = [
        str(python_executable),
        str(script),
        "--workspace",
        str(workspace),
        "--state-root",
        str(state_root),
        "--market-env",
        str(market_env),
    ]
    return {
        BACKEND_LABEL: _agent(
            label=BACKEND_LABEL,
            arguments=[*common, "run-backend"],
            workspace=workspace,
            state_root=state_root,
        ),
        FRONTEND_LABEL: _agent(
            label=FRONTEND_LABEL,
            arguments=[*common, "run-frontend"],
            workspace=workspace,
            state_root=state_root,
        ),
    }


def _launch_agent_path(label: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def _domain() -> str:
    return f"gui/{os.getuid()}"


def install_agents(*, workspace: Path, state_root: Path, market_env: Path) -> None:
    initialize_state(state_root)
    agents = render_launch_agents(
        workspace=workspace,
        state_root=state_root,
        market_env=market_env,
        python_executable=Path(sys.executable),
    )
    for label, payload in agents.items():
        path = _launch_agent_path(label)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".plist.tmp")
        temporary.write_bytes(plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True))
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        subprocess.run(
            ["launchctl", "bootout", _domain(), str(path)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(["launchctl", "bootstrap", _domain(), str(path)], check=True)


def restart_agents() -> None:
    for label in (BACKEND_LABEL, FRONTEND_LABEL):
        subprocess.run(["launchctl", "kickstart", "-k", f"{_domain()}/{label}"], check=True)


def uninstall_agents() -> None:
    for label in (BACKEND_LABEL, FRONTEND_LABEL):
        path = _launch_agent_path(label)
        subprocess.run(
            ["launchctl", "bootout", _domain(), str(path)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def status() -> int:
    result = 0
    for label in (BACKEND_LABEL, FRONTEND_LABEL):
        completed = subprocess.run(
            ["launchctl", "print", f"{_domain()}/{label}"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        running = completed.returncode == 0
        print(f"{label}: {'loaded' if running else 'not loaded'}")
        result = max(result, 0 if running else 1)
    return result


def run_backend(*, workspace: Path, state_root: Path, market_env: Path) -> None:
    environment = os.environ.copy()
    tenant_id = selected_tenant_id(environment)
    environment.update(
        backend_environment(
            state_root=state_root,
            market_env=market_env,
            tenant_id=tenant_id,
        )
    )
    campaign_config = environment.get(
        "ZEROTH_DEV_CAMPAIGN_CONFIG", "release/live_evaluation/campaign-v1.json"
    )
    port = environment.get("ZEROTH_DEV_PORT", "8122")
    arguments = [
        sys.executable,
        "-m",
        "release.live_evaluation.service",
        "--campaign-config",
        campaign_config,
        "--deployment-ref",
        selected_deployment_ref(environment),
        "--host",
        os.environ.get("ZEROTH_DEV_BIND_HOST", "127.0.0.1"),
        "--port",
        port,
    ]
    if environment.get("ZEROTH_DEV_SEED_BOOTSTRAP") == "1":
        arguments.append("--seed-bootstrap")
    if environment.get("ZEROTH_DEV_SEED_ARTIFACT_DEMO") == "1":
        arguments.append("--seed-artifact-demo")
    os.chdir(workspace)
    os.execve(sys.executable, arguments, environment)


def run_frontend(*, workspace: Path) -> None:
    os.chdir(workspace / "frontend")
    os.execvpe(
        "npm",
        ["npm", "run", "dev", "--", "--hostname", "127.0.0.1", "--port", "3000"],
        os.environ.copy(),
    )


def seed_artifact_demo(*, workspace: Path, state_root: Path, market_env: Path) -> None:
    """Seed the deterministic artifact workflow into the persistent dev database."""
    tenant_id = selected_tenant_id(os.environ)
    os.environ.update(
        backend_environment(
            state_root=state_root,
            market_env=market_env,
            tenant_id=tenant_id,
        )
    )
    os.chdir(workspace)

    async def seed() -> None:
        from release.live_evaluation.artifact_demo import seed_artifact_demo as seed_demo
        from zeroth.platform.config.settings import get_settings
        from zeroth.platform.storage.factory import create_database

        database = await create_database(get_settings())
        deployment = await seed_demo(database, tenant_id=tenant_id)
        print(
            deployment.deployment_ref,
            deployment.version,
            deployment.graph_id,
            deployment.graph_version,
        )

    asyncio.run(seed())


def seed_ambiguous_operation_demo(
    *, workspace: Path, state_root: Path, market_env: Path
) -> None:
    """Seed the inert ambiguous-operation UI fixture into the selected database."""
    tenant_id = selected_tenant_id(os.environ)
    os.environ.update(
        backend_environment(
            state_root=state_root,
            market_env=market_env,
            tenant_id=tenant_id,
        )
    )
    os.chdir(workspace)

    async def seed() -> None:
        from release.live_evaluation.ambiguous_operation_demo import (
            seed_ambiguous_operation_demo as seed_demo,
        )
        from release.live_evaluation.campaign_runtime import RepositoryTenantGraphPublisher
        from zeroth.platform.config.settings import get_settings
        from zeroth.platform.secrets import build_secret_provider
        from zeroth.platform.signing import build_signing_provider_async
        from zeroth.platform.storage.factory import create_database

        settings = get_settings()
        database = await create_database(settings)
        deployment_ref = selected_deployment_ref(os.environ)
        deployment = await RepositoryTenantGraphPublisher(database).deployment_repository.get(
            deployment_ref,
            tenant_id=tenant_id,
            workspace_id=None,
        )
        if deployment is None:
            raise RuntimeError(f"selected deployment does not exist: {deployment_ref}")
        secret_provider = build_secret_provider(settings.secrets)
        signer = await build_signing_provider_async(
            settings.provenance,
            secret_provider,
            tenant_id=tenant_id,
        )
        if signer is None:
            raise RuntimeError("ambiguous-operation demo requires provenance signing")
        fixture = await seed_demo(
            database,
            tenant_id=tenant_id,
            workspace_id=None,
            deployment_ref=deployment.deployment_ref,
            graph_version_ref=f"{deployment.graph_id}@{deployment.graph_version}",
            signer=signer,
            fixture_id="ui-validation-v1",
        )
        print(
            json.dumps(
                {
                    "run_id": fixture.run.run_id,
                    "deployment_ref": fixture.run.deployment_ref,
                    "operation_key": fixture.operation_key,
                    "operation_state": fixture.operation["state"],
                    "audit_id": fixture.audit.audit_id,
                    "signed": fixture.audit.record_signature is not None,
                },
                sort_keys=True,
            )
        )

    asyncio.run(seed())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument(
        "--state-root",
        type=Path,
        default=Path.home() / ".local/share/zeroth/evaluations/evaluation-studio-v1",
    )
    parser.add_argument("--market-env", type=Path, default=Path.cwd() / ".dev-secrets/zeroth.env")
    parser.add_argument(
        "command",
        choices=(
            "install",
            "restart",
            "status",
            "uninstall",
            "run-backend",
            "run-frontend",
            "seed-artifact-demo",
            "seed-ambiguous-operation-demo",
        ),
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "install":
        install_agents(
            workspace=args.workspace.resolve(),
            state_root=args.state_root.resolve(),
            market_env=args.market_env.resolve(),
        )
    elif args.command == "restart":
        restart_agents()
    elif args.command == "status":
        return status()
    elif args.command == "uninstall":
        uninstall_agents()
    elif args.command == "run-backend":
        run_backend(
            workspace=args.workspace.resolve(),
            state_root=args.state_root.resolve(),
            market_env=args.market_env.resolve(),
        )
    elif args.command == "run-frontend":
        run_frontend(workspace=args.workspace.resolve())
    elif args.command == "seed-artifact-demo":
        seed_artifact_demo(
            workspace=args.workspace.resolve(),
            state_root=args.state_root.resolve(),
            market_env=args.market_env.resolve(),
        )
    else:
        seed_ambiguous_operation_demo(
            workspace=args.workspace.resolve(),
            state_root=args.state_root.resolve(),
            market_env=args.market_env.resolve(),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
