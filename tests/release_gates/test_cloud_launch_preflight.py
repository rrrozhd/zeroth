from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from release.cloud_launch_preflight import (
    PreflightConfig,
    main,
    run_preflight,
)


def _transport(
    *,
    readiness_status: str = "ok",
    history: object | None = None,
    landing: str = "Economic debugger for production AI. 14-day trial $39/month",
) -> httpx.MockTransport:
    def dispatch(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        if request.url.path == "/":
            return httpx.Response(
                200,
                text=landing,
                headers={"content-type": "text/html; charset=utf-8"},
            )
        if request.url.path == "/health/ready":
            return httpx.Response(
                200,
                json={
                    "status": readiness_status,
                    "checks": {"database": {"status": "ok"}},
                    "schema_revision": {
                        "applied": "035",
                        "head": "035",
                        "state": "current",
                    },
                    "production_ready": False,
                    "certification": {"production_ready": False, "blockers": []},
                },
            )
        if request.url.path == "/v1/cloud/auth/login":
            return httpx.Response(
                307,
                headers={
                    "location": "https://api.workos.com/user_management/authorize?state=secret",
                    "set-cookie": (
                        "zeroth_auth_flow=sealed-secret; Path=/v1/cloud/auth/callback; "
                        "HttpOnly; Secure; SameSite=lax"
                    ),
                },
            )
        if request.url.path == "/v1/backtests":
            assert request.headers["authorization"] == "Bearer project-secret"
            return httpx.Response(200, json=[] if history is None else history)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    return httpx.MockTransport(dispatch)


def test_preflight_proves_readiness_authkit_and_project_key_without_writes() -> None:
    with httpx.Client(transport=_transport()) as client:
        report = run_preflight(
            PreflightConfig(base_url="https://api.zeroth.example"),
            api_key="project-secret",
            client=client,
        )

    assert report["status"] == "passed"
    assert [check["name"] for check in report["checks"]] == [
        "self_service_landing",
        "strict_readiness",
        "authkit_login",
        "project_key_history",
    ]
    serialized = json.dumps(report)
    assert "project-secret" not in serialized
    assert "sealed-secret" not in serialized
    assert "state=secret" not in serialized
    assert report["checks"][2]["detail"] == "redirect=https://api.workos.com"


def test_preflight_fails_when_readiness_body_is_degraded_despite_http_200() -> None:
    with httpx.Client(transport=_transport(readiness_status="degraded")) as client:
        report = run_preflight(
            PreflightConfig(base_url="https://api.zeroth.example"),
            api_key="project-secret",
            client=client,
        )

    assert report["status"] == "failed"
    assert report["checks"][1] == {
        "name": "strict_readiness",
        "status": "failed",
        "detail": "readiness status=degraded; schema=current",
    }


def test_preflight_fails_when_the_customer_offer_is_not_served() -> None:
    with httpx.Client(transport=_transport(landing="Not Found")) as client:
        report = run_preflight(
            PreflightConfig(base_url="https://api.zeroth.example"),
            api_key="project-secret",
            client=client,
        )

    assert report["status"] == "failed"
    assert report["checks"][0] == {
        "name": "self_service_landing",
        "status": "failed",
        "detail": "public Solo offer is incomplete",
    }


def test_preflight_rejects_a_non_list_history_response() -> None:
    with httpx.Client(transport=_transport(history={"items": []})) as client:
        report = run_preflight(
            PreflightConfig(base_url="https://api.zeroth.example"),
            api_key="project-secret",
            client=client,
        )

    assert report["status"] == "failed"
    assert report["checks"][3]["detail"] == "backtest history response is not an array"


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.example.com",
        "https://user:secret@api.example.com",
        "https://api.example.com/path",
        "https://api.example.com?token=secret",
    ],
)
def test_config_accepts_only_a_clean_https_origin(base_url: str) -> None:
    with pytest.raises(ValueError, match="HTTPS origin"):
        PreflightConfig(base_url=base_url)


def test_cli_reads_key_from_environment_and_writes_an_atomic_safe_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "release.cloud_launch_preflight._client",
        lambda _timeout: httpx.Client(transport=_transport()),
    )
    output = tmp_path / "nested" / "cloud-preflight.json"

    exit_code = main(
        ["--base-url", "https://api.zeroth.example", "--output", str(output)],
        environ={"ZEROTH_CLOUD_API_KEY": "project-secret"},
    )

    assert exit_code == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "passed"
    assert "project-secret" not in output.read_text(encoding="utf-8")
    assert output.stat().st_mode & 0o777 == 0o600
    assert list(output.parent.glob(".*.tmp")) == []


def test_cli_refuses_to_run_without_the_project_key_environment_variable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        [
            "--base-url",
            "https://api.zeroth.example",
            "--output",
            str(tmp_path / "report.json"),
        ],
        environ={},
    )

    assert exit_code == 2
    assert "ZEROTH_CLOUD_API_KEY is unset" in capsys.readouterr().err
    assert not (tmp_path / "report.json").exists()
