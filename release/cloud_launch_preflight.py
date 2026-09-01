"""Read-only black-box preflight for a candidate Zeroth Cloud deployment."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

_KEY_ENV = re.compile(r"[A-Z][A-Z0-9_]*")


@dataclass(frozen=True)
class PreflightConfig:
    """Validated non-secret inputs for the cloud launch preflight."""

    base_url: str
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("base_url must be a clean HTTPS origin")
        if self.timeout_seconds <= 0 or self.timeout_seconds > 60:
            raise ValueError("timeout_seconds must be greater than 0 and at most 60")
        object.__setattr__(self, "base_url", self.base_url.rstrip("/"))


def _check(name: str, passed: bool, detail: str) -> dict[str, str]:
    return {"name": name, "status": "passed" if passed else "failed", "detail": detail}


def _strict_readiness(config: PreflightConfig, client: httpx.Client) -> dict[str, str]:
    try:
        response = client.get(f"{config.base_url}/health/ready")
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, json.JSONDecodeError, TypeError, ValueError):
        return _check("strict_readiness", False, "readiness request failed")
    if not isinstance(payload, dict):
        return _check("strict_readiness", False, "readiness response is not an object")
    status = payload.get("status")
    revision = payload.get("schema_revision")
    schema_state = revision.get("state") if isinstance(revision, dict) else None
    passed = status == "ok" and schema_state == "current"
    return _check(
        "strict_readiness",
        passed,
        f"readiness status={status}; schema={schema_state}",
    )


def _self_service_landing(config: PreflightConfig, client: httpx.Client) -> dict[str, str]:
    try:
        response = client.get(f"{config.base_url}/")
        response.raise_for_status()
    except httpx.HTTPError:
        return _check("self_service_landing", False, "customer landing request failed")
    content_type = response.headers.get("content-type", "").lower()
    expected = ("economic debugger", "14-day trial", "$39/month")
    passed = content_type.startswith("text/html") and all(
        marker in response.text.lower() for marker in expected
    )
    return _check(
        "self_service_landing",
        passed,
        "public Solo offer is available" if passed else "public Solo offer is incomplete",
    )


def _authkit_login(config: PreflightConfig, client: httpx.Client) -> dict[str, str]:
    try:
        response = client.get(f"{config.base_url}/v1/cloud/auth/login")
    except httpx.HTTPError:
        return _check("authkit_login", False, "AuthKit login request failed")
    location = response.headers.get("location", "")
    parsed = urlsplit(location)
    cookies = "\n".join(response.headers.get_list("set-cookie")).lower()
    secure_cookie = all(
        token in cookies
        for token in ("zeroth_auth_flow=", "httponly", "secure", "samesite=lax")
    )
    passed = (
        response.status_code in {302, 303, 307, 308}
        and parsed.scheme == "https"
        and bool(parsed.netloc)
        and secure_cookie
    )
    redirect_origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else "invalid"
    return _check("authkit_login", passed, f"redirect={redirect_origin}")


def _project_key_history(
    config: PreflightConfig,
    api_key: str,
    client: httpx.Client,
) -> dict[str, str]:
    try:
        response = client.get(
            f"{config.base_url}/v1/backtests",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        response.raise_for_status()
        payload: Any = response.json()
    except (httpx.HTTPError, json.JSONDecodeError, TypeError, ValueError):
        return _check("project_key_history", False, "project-key history request failed")
    if not isinstance(payload, list):
        return _check(
            "project_key_history", False, "backtest history response is not an array"
        )
    return _check("project_key_history", True, f"retained_backtests={len(payload)}")


def run_preflight(
    config: PreflightConfig,
    *,
    api_key: str,
    client: httpx.Client,
) -> dict[str, Any]:
    """Run only read-only checks and return a secret-free evidence report."""
    if not api_key.strip():
        raise ValueError("api_key must not be empty")
    checks = [
        _self_service_landing(config, client),
        _strict_readiness(config, client),
        _authkit_login(config, client),
        _project_key_history(config, api_key, client),
    ]
    return {
        "schema_version": 1,
        "status": (
            "passed" if all(check["status"] == "passed" for check in checks) else "failed"
        ),
        "target_origin": config.base_url,
        "finished_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "checks": checks,
        "limitations": [
            "read-only preflight; no Paddle transaction was created",
            "does not replace sandbox or production purchase acceptance",
        ],
    }


def _client(timeout_seconds: float) -> httpx.Client:
    return httpx.Client(timeout=timeout_seconds, follow_redirects=False)


def _write_report_atomic(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run read-only launch checks against a hosted Zeroth candidate."
    )
    parser.add_argument("--base-url", required=True, help="candidate HTTPS origin")
    parser.add_argument("--output", required=True, type=Path, help="JSON evidence path")
    parser.add_argument(
        "--api-key-env",
        default="ZEROTH_CLOUD_API_KEY",
        help="environment variable containing the one-time project key",
    )
    parser.add_argument("--timeout", type=float, default=10.0)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> int:
    args = _parser().parse_args(argv)
    source = os.environ if environ is None else environ
    if _KEY_ENV.fullmatch(args.api_key_env) is None:
        print("preflight configuration failed: invalid API key environment name", file=sys.stderr)
        return 2
    api_key = source.get(args.api_key_env, "")
    if not api_key:
        print(f"preflight configuration failed: {args.api_key_env} is unset", file=sys.stderr)
        return 2
    try:
        config = PreflightConfig(base_url=args.base_url, timeout_seconds=args.timeout)
    except ValueError as exc:
        print(f"preflight configuration failed: {exc}", file=sys.stderr)
        return 2
    with _client(config.timeout_seconds) as client:
        report = run_preflight(config, api_key=api_key, client=client)
    _write_report_atomic(args.output, report)
    print(f"cloud launch preflight {report['status']}: {args.output}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
