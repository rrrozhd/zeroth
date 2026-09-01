from __future__ import annotations

import inspect

import pytest

from zeroth.econ.plane import config
from zeroth.econ.plane.common import bootstrap as bootstrap_module
from zeroth.econ.plane.database import get_db


def test_request_database_dependency_does_not_bootstrap_schema() -> None:
    request_source = inspect.getsource(get_db)
    startup_source = inspect.getsource(bootstrap_module.bootstrap)
    assert "create_all" not in request_source
    assert "ensure_sqlite_compat" not in request_source
    assert "create_all" in startup_source
    assert "ensure_sqlite_compat" in startup_source


def test_standalone_startup_refuses_placeholder_jwt_secret(monkeypatch) -> None:
    monkeypatch.setattr(config.settings, "jwt_secret", "change-me")
    with pytest.raises(config.EconConfigError, match="ECP_JWT_SECRET"):
        config.validate_startup_settings()


@pytest.mark.parametrize("secret", ["", "   "])
def test_standalone_startup_refuses_blank_jwt_secret(monkeypatch, secret: str) -> None:
    monkeypatch.setattr(config.settings, "jwt_secret", secret)
    with pytest.raises(config.EconConfigError, match="ECP_JWT_SECRET"):
        config.validate_startup_settings()


def test_standalone_startup_accepts_configured_jwt_secret(monkeypatch) -> None:
    monkeypatch.setattr(config.settings, "jwt_secret", "configured-secret")
    config.validate_startup_settings()


def test_hosted_startup_requires_complete_workos_configuration(monkeypatch) -> None:
    monkeypatch.setattr(config.settings, "jwt_secret", "configured-secret")
    monkeypatch.setattr(config.settings, "workos_authkit_enabled", True)
    monkeypatch.setattr(config.settings, "workos_client_id", "client_test")
    monkeypatch.setattr(config.settings, "workos_api_key", "")
    monkeypatch.setattr(config.settings, "workos_redirect_uri", "https://api.example.test/callback")
    monkeypatch.setattr(config.settings, "workos_cookie_password", "x" * 32)
    monkeypatch.setattr(config.settings, "cloud_browser_origin", "https://app.example.test")

    with pytest.raises(config.EconConfigError, match="ECP_WORKOS_API_KEY"):
        config.validate_startup_settings()


def test_hosted_startup_rejects_short_cookie_password(monkeypatch) -> None:
    monkeypatch.setattr(config.settings, "jwt_secret", "configured-secret")
    monkeypatch.setattr(config.settings, "workos_authkit_enabled", True)
    monkeypatch.setattr(config.settings, "workos_client_id", "client_test")
    monkeypatch.setattr(config.settings, "workos_api_key", "sk_test")
    monkeypatch.setattr(config.settings, "workos_redirect_uri", "https://api.example.test/callback")
    monkeypatch.setattr(config.settings, "workos_cookie_password", "too-short")
    monkeypatch.setattr(config.settings, "cloud_browser_origin", "https://app.example.test")

    with pytest.raises(config.EconConfigError, match="at least 32"):
        config.validate_startup_settings()


def test_hosted_startup_requires_same_origin_browser_and_authkit_callback(monkeypatch) -> None:
    monkeypatch.setattr(config.settings, "jwt_secret", "configured-secret")
    monkeypatch.setattr(config.settings, "workos_authkit_enabled", True)
    monkeypatch.setattr(config.settings, "workos_client_id", "client_test")
    monkeypatch.setattr(config.settings, "workos_api_key", "sk_test")
    monkeypatch.setattr(
        config.settings,
        "workos_redirect_uri",
        "https://api.example.test/v1/cloud/auth/callback",
    )
    monkeypatch.setattr(config.settings, "workos_cookie_password", "x" * 32)
    monkeypatch.setattr(config.settings, "cloud_browser_origin", "https://app.example.test")

    with pytest.raises(config.EconConfigError, match="same HTTPS origin"):
        config.validate_startup_settings()

    monkeypatch.setattr(config.settings, "cloud_browser_origin", "https://api.example.test")
    config.validate_startup_settings()


def test_hosted_startup_requires_paddle_price_and_signature_configuration(monkeypatch) -> None:
    monkeypatch.setattr(config.settings, "jwt_secret", "configured-secret")
    monkeypatch.setattr(config.settings, "workos_authkit_enabled", False)
    monkeypatch.setattr(config.settings, "paddle_billing_enabled", True)
    monkeypatch.setattr(config.settings, "paddle_api_key", "pdl_test")
    monkeypatch.setattr(config.settings, "paddle_webhook_secret", "")
    monkeypatch.setattr(config.settings, "paddle_solo_price_id", "pri_solo")
    monkeypatch.setattr(config.settings, "paddle_team_price_id", "pri_team")

    with pytest.raises(config.EconConfigError, match="ECP_PADDLE_WEBHOOK_SECRET"):
        config.validate_startup_settings()


def test_solo_only_paddle_startup_does_not_require_a_team_price(monkeypatch) -> None:
    monkeypatch.setattr(config.settings, "jwt_secret", "configured-secret")
    monkeypatch.setattr(config.settings, "workos_authkit_enabled", False)
    monkeypatch.setattr(config.settings, "paddle_billing_enabled", True)
    monkeypatch.setattr(config.settings, "paddle_api_key", "pdl_test")
    monkeypatch.setattr(config.settings, "paddle_webhook_secret", "pdl_webhook_test")
    monkeypatch.setattr(config.settings, "paddle_solo_price_id", "pri_solo")
    monkeypatch.setattr(config.settings, "paddle_team_price_id", "")

    config.validate_startup_settings()


def test_cloud_scheduler_requires_entitlement_enforcement(monkeypatch) -> None:
    monkeypatch.setattr(config.settings, "jwt_secret", "configured-secret")
    monkeypatch.setattr(config.settings, "cloud_scheduler_enabled", True)
    monkeypatch.setattr(config.settings, "cloud_entitlements_enabled", False)

    with pytest.raises(config.EconConfigError, match="scheduler requires cloud entitlements"):
        config.validate_startup_settings()
