"""Import-safe environment boundary for the disposable economics UI fixture."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

PRIMARY_STATE_ROOT = (
    Path.home() / ".local/share/zeroth/evaluations/evaluation-studio-v1"
).resolve()
FIXTURE_TENANT = "evaluation-studio-v1"
FIXTURE_DEPLOYMENT = "economics-ui-fixture-v1"
UI_ACCESS_VALUE = "economics-ui-fixture-browser-v1"


def assert_disposable_state_root(state_root: Path) -> Path:
    resolved = state_root.expanduser().resolve(strict=False)
    if resolved == PRIMARY_STATE_ROOT:
        raise ValueError("refusing the primary campaign state root")
    if not resolved.name.startswith("economics-ui-fixture-"):
        raise ValueError("state root basename must start with economics-ui-fixture-")
    if resolved in {Path("/").resolve(), Path.home().resolve()}:
        raise ValueError("fixture state root must be a narrow child directory")
    return resolved


def _loopback_origin(origin: str) -> str:
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ValueError("console origin must use a loopback host")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("console origin must not contain credentials, query, or fragment")
    return origin.rstrip("/")


def fixture_environment(
    state_root: Path,
    *,
    console_origin: str = "http://127.0.0.1:3000",
) -> dict[str, str]:
    root = assert_disposable_state_root(state_root)
    origin = _loopback_origin(console_origin)
    credentials = [
        {
            "credential_id": "economics-ui-fixture-browser",
            "secret": UI_ACCESS_VALUE,
            "subject": "economics-ui-fixture-browser",
            "roles": ["admin"],
            "tenant_id": FIXTURE_TENANT,
        }
    ]
    return {
        "ZEROTH_DATABASE__BACKEND": "sqlite",
        "ZEROTH_DATABASE__SQLITE_PATH": str(root / "zeroth.db"),
        "ECP_DATABASE_URL": f"sqlite+pysqlite:///{root / 'econ.db'}",
        "ECP_JWT_SECRET": "economics-ui-fixture-jwt-v1",
        "ECP_SERVICE_PRINCIPAL_TENANT_ID": FIXTURE_TENANT,
        "ZEROTH_SERVICE_API_KEYS_JSON": json.dumps(credentials, separators=(",", ":")),
        "ZEROTH_AUTH__BROWSER_SESSION_SECRET": (
            "economics-ui-fixture-browser-session-secret-v1-32-bytes"
        ),
        "ZEROTH_SECRETS__BACKEND": "env",
        "ZEROTH_SECRETS__ALLOW_ENV_FALLBACK": "false",
        "ZEROTH_PROVENANCE__MODE": "off",
        "ZEROTH_REGULUS__FAIL_CLOSED": "false",
        "ZEROTH_AUTO_AGENT_RUNNERS": "false",
        "ZEROTH_TENANT_ID": FIXTURE_TENANT,
        "ZEROTH_DEPLOYMENT_REF": FIXTURE_DEPLOYMENT,
        "ZEROTH_CONSOLE_CORS_ORIGINS": origin,
        "ZEROTH_ARTIFACT_STORE__FILESYSTEM_BASE_DIR": str(root / "runtime-artifacts"),
        "ZEROTH_WEBHOOK__ENABLED": "false",
        "ZEROTH_APPROVAL_SLA__ENABLED": "false",
        "ZEROTH_RETENTION__ENABLED": "false",
    }
