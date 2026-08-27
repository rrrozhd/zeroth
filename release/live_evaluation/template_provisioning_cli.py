"""Provider-free, fail-closed provisioning for the live-template fixture.

This command deliberately has no provider credential, execution, or service
lifecycle input.  It only publishes the fixed disposable fixture against a
numeric loopback Zeroth service while the frozen D-012 serving identity remains
unchanged.
"""

from __future__ import annotations

import argparse
import contextlib
import ipaddress
import json
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from release.live_evaluation.template_live_rendered_execution import (
    LiveTemplateConfig,
    LiveTemplateHarness,
    Response,
)

EXACT_CONFIG = LiveTemplateConfig(
    fixture_id="live-template-render-20260826",
    tenant_id="evaluation-studio-v1",
    template_name="live-template-render-20260826",
    deployment_ref="live-template-render-20260826-v1",
)
FROZEN_D012_IDENTITY: dict[str, object] = {
    "deployment_ref": "provider-free-child-approval-d012-20260826-2-parent",
    "deployment_version": 1,
    "graph_version_ref": "0179d403-2863-45f3-9556-58052a992da8@1",
}
_WORKFLOW_NAME = f"Live template render {EXACT_CONFIG.fixture_id}"
_CONTRACT_NAMES = {
    f"contract://{EXACT_CONFIG.fixture_id}.probe",
    f"contract://{EXACT_CONFIG.fixture_id}.answer",
}


class ProvisioningBlockedError(RuntimeError):
    """A stable, non-sensitive fail-closed reason."""


class _SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        self.exit(2, "error: invalid arguments\n")


class _SanitizedResponse:
    """Expose status and JSON, but never an HTTP response body as error text."""

    text = "<redacted>"

    def __init__(self, response: httpx.Response) -> None:
        self.status_code = response.status_code
        self._response = response

    def json(self) -> object:
        return self._response.json()


class LoopbackServiceRequest:
    """Authenticated request adapter whose only secret is the Zeroth service key."""

    def __init__(self, *, base_url: str, service_api_key_file: Path) -> None:
        self._base_url = validate_loopback_base_url(base_url)
        repository_root = Path(__file__).resolve().parents[2]
        key_path = validate_service_api_key_file(
            service_api_key_file, repository_root=repository_root
        )
        try:
            service_key = key_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ProvisioningBlockedError("service-key-file-unreadable") from exc
        if not service_key or "\n" in service_key or "\r" in service_key or len(service_key) > 4096:
            raise ProvisioningBlockedError("service-key-file-invalid")
        self._client = httpx.Client(
            base_url=self._base_url,
            headers={
                "Accept": "application/json",
                "X-API-Key": service_key,
                "X-Tenant-ID": EXACT_CONFIG.tenant_id,
            },
            timeout=httpx.Timeout(15.0),
            follow_redirects=False,
        )

    def __call__(self, method: str, path: str, payload: dict[str, Any] | None) -> Response:
        if method not in {"GET", "POST", "PUT"} or not path.startswith("/"):
            raise ProvisioningBlockedError("request-contract-violation")
        try:
            response = self._client.request(method, path, json=payload)
        except httpx.HTTPError as exc:
            raise ProvisioningBlockedError("service-request-failed") from exc
        return _SanitizedResponse(response)

    def close(self) -> None:
        self._client.close()


Request = Callable[[str, str, dict[str, Any] | None], Response]


def validate_loopback_base_url(value: str) -> str:
    parsed = urlsplit(value)
    try:
        address = ipaddress.ip_address(parsed.hostname or "")
        port = parsed.port
    except ValueError as exc:
        raise ProvisioningBlockedError("service-base-url-not-loopback") from exc
    if (
        parsed.scheme != "http"
        or not address.is_loopback
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ProvisioningBlockedError("service-base-url-not-loopback")
    return f"http://{address.compressed}:{port}"


def validate_service_api_key_file(path: Path, *, repository_root: Path) -> Path:
    if not path.is_absolute() or path.is_symlink():
        raise ProvisioningBlockedError("service-key-file-not-exact")
    try:
        resolved = path.resolve(strict=True)
        repository = repository_root.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as exc:
        raise ProvisioningBlockedError("service-key-file-unavailable") from exc
    if resolved.is_relative_to(repository):
        raise ProvisioningBlockedError("service-key-file-inside-repository")
    if not stat.S_ISREG(metadata.st_mode):
        raise ProvisioningBlockedError("service-key-file-not-regular")
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ProvisioningBlockedError("service-key-file-not-private")
    return resolved


def _object(response: Response, *, label: str) -> object:
    if response.status_code != 200:
        raise ProvisioningBlockedError(f"{label}-lookup-failed")
    try:
        return response.json()
    except Exception as exc:
        raise ProvisioningBlockedError(f"{label}-lookup-malformed") from exc


def _health(request: Request) -> dict[str, object]:
    value = _object(request("GET", "/health", None), label="health")
    if not isinstance(value, Mapping):
        raise ProvisioningBlockedError("health-lookup-malformed")
    health = {
        "status": value.get("status"),
        "deployment_ref": value.get("deployment_ref"),
        "deployment_version": value.get("deployment_version"),
        "graph_version_ref": value.get("graph_version_ref"),
    }
    if health != {"status": "ok", **FROZEN_D012_IDENTITY}:
        raise ProvisioningBlockedError("frozen-d012-mismatch")
    return health


def _list(request: Request, path: str, *, label: str) -> list[object]:
    value = _object(request("GET", path, None), label=label)
    if path == "/v1/templates":
        if not isinstance(value, Mapping):
            raise ProvisioningBlockedError(f"{label}-lookup-malformed")
        value = value.get("templates")
    if not isinstance(value, list):
        raise ProvisioningBlockedError(f"{label}-lookup-malformed")
    return value


def _assert_fixture_absent(request: Request) -> None:
    templates = _list(request, "/v1/templates", label="template")
    contracts = _list(request, "/api/studio/v1/contracts", label="contract")
    workflows = _list(request, "/api/studio/v1/workflows", label="workflow")
    deployments = _list(request, "/v1/deployments", label="deployment")
    collision = (
        any(
            isinstance(row, Mapping) and row.get("name") == EXACT_CONFIG.template_name
            for row in templates
        )
        or any(isinstance(row, Mapping) and row.get("name") in _CONTRACT_NAMES for row in contracts)
        or any(isinstance(row, Mapping) and row.get("name") == _WORKFLOW_NAME for row in workflows)
        or any(
            isinstance(row, Mapping) and row.get("deployment_ref") == EXACT_CONFIG.deployment_ref
            for row in deployments
        )
    )
    if collision:
        raise ProvisioningBlockedError("fixture-preexists")


def provision_live_template(*, request: Request) -> dict[str, object]:
    pre_health = _health(request)
    _assert_fixture_absent(request)
    harness = LiveTemplateHarness(
        config=EXACT_CONFIG,
        request=request,
        secret_reference_available=lambda _reference, _tenant: False,
        environment={},
    )
    try:
        fixture = harness.provision()
    except ProvisioningBlockedError:
        raise
    except Exception as exc:
        raise ProvisioningBlockedError("fixture-provisioning-failed") from exc
    post_health = _health(request)
    if post_health != pre_health:
        raise ProvisioningBlockedError("frozen-d012-changed")
    return {
        "status": "provisioned",
        "config": asdict(EXACT_CONFIG),
        "pre_health": pre_health,
        "post_health": post_health,
        "fixture": asdict(fixture),
        "provider_calls_performed": 0,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = _SafeParser(description="Provision the fixed provider-free live-template fixture")
    parser.add_argument("--service-base-url", required=True)
    parser.add_argument("--service-api-key-file", required=True, type=Path)
    parser.add_argument("--output-json", type=Path)
    return parser


def _write_output(path: Path, result: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(result, stream, sort_keys=True, indent=2)
            stream.write("\n")
    except Exception:
        with contextlib.suppress(OSError):
            path.unlink()
        raise


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    request: LoopbackServiceRequest | None = None
    try:
        request = LoopbackServiceRequest(
            base_url=args.service_base_url,
            service_api_key_file=args.service_api_key_file,
        )
        result = provision_live_template(request=request)
        if args.output_json is not None:
            _write_output(args.output_json, result)
        print(json.dumps(result, sort_keys=True))
        return 0
    except ProvisioningBlockedError as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, sort_keys=True))
        return 2
    except Exception:
        print(json.dumps({"status": "blocked", "reason": "internal-error"}, sort_keys=True))
        return 2
    finally:
        if request is not None:
            request.close()


if __name__ == "__main__":
    raise SystemExit(main())
