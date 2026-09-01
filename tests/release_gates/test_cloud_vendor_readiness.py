from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from release.cloud_vendor_readiness import (
    VendorReadinessConfig,
    main,
    run_vendor_readiness,
)


ORIGIN = "https://cloud.example.com"
REDIRECT_URI = f"{ORIGIN}/v1/cloud/auth/callback"
PRICE_ID = "pri_01gsz8z1q1n00f12qt82y31smh"
NOTIFICATION_ID = "ntfset_01gt21c5pdx9q1e4mh1xrsjjn6"
SUBSCRIPTION_EVENTS = {
    "subscription.activated",
    "subscription.canceled",
    "subscription.created",
    "subscription.past_due",
    "subscription.paused",
    "subscription.resumed",
    "subscription.trialing",
    "subscription.updated",
}


def _config() -> VendorReadinessConfig:
    return VendorReadinessConfig(
        public_origin=ORIGIN,
        workos_api_key="workos-secret",
        workos_redirect_uri=REDIRECT_URI,
        paddle_api_key="paddle-secret",
        paddle_solo_price_id=PRICE_ID,
        paddle_notification_setting_id=NOTIFICATION_ID,
        paddle_sandbox=True,
    )


def _response(request: httpx.Request, *, bad: str | None = None) -> httpx.Response:
    if request.url.host == "api.workos.com" and request.url.path.endswith("redirect_uris"):
        data = [{"id": "redir_1", "uri": REDIRECT_URI, "default": True}]
        if bad == "redirect":
            data[0]["uri"] = f"{ORIGIN}/wrong"
        return httpx.Response(200, json={"object": "list", "data": data})
    if request.url.host == "api.workos.com" and request.url.path.endswith("roles"):
        roles = ["admin", "analyst", "approver", "viewer"]
        if bad == "roles":
            roles.remove("admin")
        return httpx.Response(200, json={"object": "list", "data": [{"slug": x} for x in roles]})
    if request.url.path.startswith("/prices/"):
        price = {
            "id": PRICE_ID,
            "type": "standard",
            "status": "active",
            "unit_price": {"amount": "3900", "currency_code": "USD"},
            "billing_cycle": {"interval": "month", "frequency": 1},
            "trial_period": {
                "interval": "day",
                "frequency": 14,
                "requires_payment_method": True,
                "unit_price": None,
            },
            "product": {"id": "pro_1", "name": "Zeroth Solo", "status": "active"},
        }
        if bad == "price":
            price["unit_price"]["amount"] = "4900"
        return httpx.Response(200, json={"data": price, "meta": {"request_id": "req_1"}})
    if request.url.path.startswith("/notification-settings/"):
        events = sorted(SUBSCRIPTION_EVENTS)
        if bad == "webhook":
            events.remove("subscription.canceled")
        return httpx.Response(
            200,
            json={
                "data": {
                    "id": NOTIFICATION_ID,
                    "type": "url",
                    "destination": f"{ORIGIN}/v1/cloud/billing/paddle/webhook",
                    "active": True,
                    "include_sensitive_fields": False,
                    "subscribed_events": [{"name": event} for event in events],
                }
            },
        )
    raise AssertionError(f"unexpected request: {request.method} {request.url}")


def _run(*, bad: str | None = None) -> dict[str, object]:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.headers["authorization"].startswith("Bearer ")
        return _response(request, bad=bad)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        return run_vendor_readiness(_config(), client=client)


def test_vendor_readiness_passes_only_for_the_approved_solo_contract() -> None:
    report = _run()

    assert report["status"] == "passed"
    assert [check["name"] for check in report["checks"]] == [
        "workos_redirect_uri",
        "workos_environment_roles",
        "paddle_solo_price",
        "paddle_webhook_destination",
    ]
    encoded = json.dumps(report)
    assert "workos-secret" not in encoded
    assert "paddle-secret" not in encoded


@pytest.mark.parametrize("bad", ["redirect", "roles", "price", "webhook"])
def test_vendor_readiness_fails_closed_for_commercial_configuration_drift(bad: str) -> None:
    report = _run(bad=bad)

    assert report["status"] == "failed"
    assert sum(check["status"] == "failed" for check in report["checks"]) == 1


def test_vendor_readiness_reports_vendor_errors_without_leaking_response_bodies() -> None:
    secret_body = "upstream-secret-body"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=secret_body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = run_vendor_readiness(_config(), client=client)

    assert report["status"] == "failed"
    assert secret_body not in json.dumps(report)
    assert all(check["detail"] == "vendor request failed" for check in report["checks"])


def test_solo_launch_does_not_require_unused_team_roles() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.workos.com" and request.url.path.endswith("roles"):
            return httpx.Response(
                200,
                json={"object": "list", "data": [{"slug": "admin"}]},
            )
        return _response(request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = run_vendor_readiness(_config(), client=client)

    assert report["status"] == "passed"


def test_cli_requires_all_secret_inputs_before_network_or_evidence_write(tmp_path: Path) -> None:
    output = tmp_path / "vendor-readiness.json"

    result = main(
        ["--public-origin", ORIGIN, "--output", str(output), "--sandbox"],
        environ={},
    )

    assert result == 2
    assert not output.exists()


def test_cli_writes_a_secret_free_mode_0600_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_client = httpx.Client

    def client_factory(**_kwargs: object) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(_response))

    monkeypatch.setattr("release.cloud_vendor_readiness.httpx.Client", client_factory)
    output = tmp_path / "nested" / "vendor-readiness.json"
    result = main(
        ["--public-origin", ORIGIN, "--output", str(output), "--sandbox"],
        environ={
            "ECP_WORKOS_API_KEY": "workos-secret",
            "ECP_WORKOS_REDIRECT_URI": REDIRECT_URI,
            "ECP_PADDLE_API_KEY": "paddle-secret",
            "ECP_PADDLE_SOLO_PRICE_ID": PRICE_ID,
            "ZEROTH_LAUNCH_PADDLE_NOTIFICATION_SETTING_ID": NOTIFICATION_ID,
        },
    )

    assert result == 0
    assert json.loads(output.read_text())["status"] == "passed"
    assert output.stat().st_mode & 0o777 == 0o600
    assert "secret" not in output.read_text()
    assert list(output.parent.glob(".*.tmp")) == []


def test_config_rejects_non_https_or_mismatched_callback() -> None:
    with pytest.raises(ValueError, match="clean HTTPS origin"):
        VendorReadinessConfig(
            **{**_config().__dict__, "public_origin": "http://cloud.example.com"}
        )
    with pytest.raises(ValueError, match="callback"):
        VendorReadinessConfig(
            **{**_config().__dict__, "workos_redirect_uri": f"{ORIGIN}/wrong"}
        )
