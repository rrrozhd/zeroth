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
