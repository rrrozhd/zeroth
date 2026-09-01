"""Read-only verification of hosted identity and commerce configuration."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

_PRICE_ID = re.compile(r"pri_[a-z\d]{26}")
_NOTIFICATION_ID = re.compile(r"ntfset_[a-z\d]{26}")
_REQUIRED_ROLES = frozenset({"admin"})
_REQUIRED_SUBSCRIPTION_EVENTS = frozenset(
    {
        "subscription.activated",
        "subscription.canceled",
        "subscription.created",
        "subscription.past_due",
        "subscription.paused",
        "subscription.resumed",
        "subscription.trialing",
        "subscription.updated",
    }
)


@dataclass(frozen=True)
class VendorReadinessConfig:
    public_origin: str
    workos_api_key: str = field(repr=False)
    workos_redirect_uri: str
    paddle_api_key: str = field(repr=False)
    paddle_solo_price_id: str
    paddle_notification_setting_id: str
    paddle_sandbox: bool

    def __post_init__(self) -> None:
        origin = urlsplit(self.public_origin)
        if (
            origin.scheme != "https"
            or not origin.netloc
            or origin.username is not None
            or origin.password is not None
            or origin.path not in {"", "/"}
            or origin.query
            or origin.fragment
        ):
            raise ValueError("public_origin must be a clean HTTPS origin")
        clean_origin = self.public_origin.rstrip("/")
        expected_callback = f"{clean_origin}/v1/cloud/auth/callback"
        if self.workos_redirect_uri != expected_callback:
            raise ValueError("WorkOS callback must exactly match the public cloud callback")
        if not self.workos_api_key or not self.paddle_api_key:
            raise ValueError("vendor API keys must not be empty")
        if _PRICE_ID.fullmatch(self.paddle_solo_price_id) is None:
            raise ValueError("Paddle Solo price ID is invalid")
        if _NOTIFICATION_ID.fullmatch(self.paddle_notification_setting_id) is None:
            raise ValueError("Paddle notification setting ID is invalid")
        object.__setattr__(self, "public_origin", clean_origin)


def _check(name: str, passed: bool, detail: str) -> dict[str, str]:
    return {"name": name, "status": "passed" if passed else "failed", "detail": detail}


def _get_json(
    client: httpx.Client,
    url: str,
    *,
    api_key: str,
) -> Mapping[str, Any] | None:
    try:
        response = client.get(url, headers={"Authorization": f"Bearer {api_key}"})
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _workos_redirect(config: VendorReadinessConfig, client: httpx.Client) -> dict[str, str]:
    payload = _get_json(
        client,
        "https://api.workos.com/user_management/redirect_uris",
        api_key=config.workos_api_key,
    )
    if payload is None:
        return _check("workos_redirect_uri", False, "vendor request failed")
    data = payload.get("data")
    configured = {
        item.get("uri")
        for item in data
        if isinstance(data, list) and isinstance(item, Mapping)
    } if isinstance(data, list) else set()
    passed = config.workos_redirect_uri in configured
    return _check(
        "workos_redirect_uri",
        passed,
        "exact callback is registered" if passed else "exact callback is not registered",
    )


def _workos_roles(config: VendorReadinessConfig, client: httpx.Client) -> dict[str, str]:
    payload = _get_json(
        client,
        "https://api.workos.com/authorization/roles",
        api_key=config.workos_api_key,
    )
    if payload is None:
        return _check("workos_environment_roles", False, "vendor request failed")
    data = payload.get("data")
    slugs = {
        str(item.get("slug", ""))
        for item in data
        if isinstance(data, list) and isinstance(item, Mapping)
    } if isinstance(data, list) else set()
    missing = sorted(_REQUIRED_ROLES - slugs)
    return _check(
        "workos_environment_roles",
        not missing,
        (
            "required role slugs are configured"
            if not missing
            else f"missing roles={','.join(missing)}"
        ),
    )


def _paddle_origin(sandbox: bool) -> str:
    return "https://sandbox-api.paddle.com" if sandbox else "https://api.paddle.com"


def _paddle_price(config: VendorReadinessConfig, client: httpx.Client) -> dict[str, str]:
    payload = _get_json(
        client,
        f"{_paddle_origin(config.paddle_sandbox)}/prices/{config.paddle_solo_price_id}?include=product",
        api_key=config.paddle_api_key,
    )
    if payload is None:
        return _check("paddle_solo_price", False, "vendor request failed")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return _check("paddle_solo_price", False, "vendor response is invalid")
    unit = data.get("unit_price")
    billing = data.get("billing_cycle")
    trial = data.get("trial_period")
    product = data.get("product")
    passed = (
        data.get("id") == config.paddle_solo_price_id
        and data.get("type") == "standard"
        and data.get("status") == "active"
        and isinstance(unit, Mapping)
        and unit.get("amount") == "3900"
        and unit.get("currency_code") == "USD"
        and isinstance(billing, Mapping)
        and billing.get("interval") == "month"
        and billing.get("frequency") == 1
        and isinstance(trial, Mapping)
        and trial.get("interval") == "day"
        and trial.get("frequency") == 14
        and trial.get("requires_payment_method") is True
        and trial.get("unit_price") is None
        and isinstance(product, Mapping)
        and product.get("status") == "active"
    )
    return _check(
        "paddle_solo_price",
        passed,
        "$39 USD monthly with a free 14-day payment-method trial"
        if passed
        else "price does not match the approved Solo contract",
    )


def _paddle_webhook(config: VendorReadinessConfig, client: httpx.Client) -> dict[str, str]:
    payload = _get_json(
        client,
        f"{_paddle_origin(config.paddle_sandbox)}/notification-settings/"
        f"{config.paddle_notification_setting_id}",
        api_key=config.paddle_api_key,
    )
    if payload is None:
        return _check("paddle_webhook_destination", False, "vendor request failed")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return _check("paddle_webhook_destination", False, "vendor response is invalid")
    subscribed = data.get("subscribed_events")
    event_names = {
        str(item.get("name", ""))
        for item in subscribed
        if isinstance(subscribed, list) and isinstance(item, Mapping)
    } if isinstance(subscribed, list) else set()
    expected = f"{config.public_origin}/v1/cloud/billing/paddle/webhook"
    passed = (
        data.get("id") == config.paddle_notification_setting_id
        and data.get("type") == "url"
        and data.get("destination") == expected
        and data.get("active") is True
        and data.get("include_sensitive_fields") is False
        and event_names >= _REQUIRED_SUBSCRIPTION_EVENTS
    )
    return _check(
        "paddle_webhook_destination",
        passed,
        "active, non-sensitive lifecycle webhook is configured"
        if passed
        else "webhook destination or lifecycle subscriptions are incomplete",
    )


def run_vendor_readiness(
    config: VendorReadinessConfig,
    *,
    client: httpx.Client,
) -> dict[str, object]:
    checks = [
        _workos_redirect(config, client),
        _workos_roles(config, client),
        _paddle_price(config, client),
        _paddle_webhook(config, client),
    ]
    return {
        "schema_version": 1,
        "status": "passed" if all(item["status"] == "passed" for item in checks) else "failed",
        "environment": "sandbox" if config.paddle_sandbox else "production",
        "target_origin": config.public_origin,
        "finished_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "checks": checks,
        "limitations": [
            "read-only vendor configuration audit; no user or transaction was created",
            "does not replace the end-to-end sandbox or production purchase journey",
        ],
    }


def _write_report_atomic(path: Path, report: Mapping[str, object]) -> None:
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
        description="Verify WorkOS and Paddle launch configuration without mutation."
    )
    parser.add_argument("--public-origin", required=True)
    parser.add_argument("--output", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--sandbox", action="store_true")
    mode.add_argument("--production", action="store_true")
    parser.add_argument("--timeout", type=float, default=10.0)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> int:
    args = _parser().parse_args(argv)
    source = os.environ if environ is None else environ
    required = {
        "workos_api_key": "ECP_WORKOS_API_KEY",
        "workos_redirect_uri": "ECP_WORKOS_REDIRECT_URI",
        "paddle_api_key": "ECP_PADDLE_API_KEY",
        "paddle_solo_price_id": "ECP_PADDLE_SOLO_PRICE_ID",
        "paddle_notification_setting_id": "ZEROTH_LAUNCH_PADDLE_NOTIFICATION_SETTING_ID",
    }
    missing = [env_name for env_name in required.values() if not source.get(env_name, "").strip()]
    if missing:
        print(
            "vendor readiness configuration failed: missing " + ", ".join(missing),
            file=sys.stderr,
        )
        return 2
    if args.timeout <= 0 or args.timeout > 60:
        print("vendor readiness configuration failed: timeout must be in (0, 60]", file=sys.stderr)
        return 2
    try:
        config = VendorReadinessConfig(
            public_origin=args.public_origin,
            workos_api_key=source[required["workos_api_key"]],
            workos_redirect_uri=source[required["workos_redirect_uri"]],
            paddle_api_key=source[required["paddle_api_key"]],
            paddle_solo_price_id=source[required["paddle_solo_price_id"]],
            paddle_notification_setting_id=source[required["paddle_notification_setting_id"]],
            paddle_sandbox=args.sandbox,
        )
    except ValueError as exc:
        print(f"vendor readiness configuration failed: {exc}", file=sys.stderr)
        return 2
    with httpx.Client(timeout=args.timeout, follow_redirects=False) as client:
        report = run_vendor_readiness(config, client=client)
    _write_report_atomic(args.output, report)
    print(f"cloud vendor readiness {report['status']}: {args.output}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
