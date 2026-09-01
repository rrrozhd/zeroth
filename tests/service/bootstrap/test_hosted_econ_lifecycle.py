"""Hosted economics must be a startup dependency, not a best-effort add-on."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from zeroth.econ.plane import config
from zeroth.econ.plane.common import bootstrap as bootstrap_module
from zeroth.econ.plane.connectors import service as connector_service
from zeroth.service.bootstrap.lifecycle import service_lifespan


def _app_with_bundled_economics() -> FastAPI:
    app = FastAPI()
    app.state.bootstrap = SimpleNamespace(regulus_client=SimpleNamespace(stop=lambda: None))
    return app


def _disable_hosted_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.settings, "workos_authkit_enabled", False)
    monkeypatch.setattr(config.settings, "paddle_billing_enabled", False)
    monkeypatch.setattr(config.settings, "cloud_entitlements_enabled", False)


@pytest.mark.asyncio
async def test_self_hosted_economics_still_degrades_when_bootstrap_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_hosted_mode(monkeypatch)
    monkeypatch.setattr(config, "validate_startup_settings", lambda: None)
    monkeypatch.setattr(
        bootstrap_module,
        "bootstrap",
        lambda: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )
    monkeypatch.setattr(connector_service, "init_otel_metrics", lambda: None)
    app = _app_with_bundled_economics()

    async with service_lifespan(app):
        assert app.state.regulus_registration_ready is False


@pytest.mark.asyncio
async def test_hosted_economics_refuses_startup_when_bootstrap_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_hosted_mode(monkeypatch)
    monkeypatch.setattr(config.settings, "cloud_entitlements_enabled", True)
    monkeypatch.setattr(config, "validate_startup_settings", lambda: None)
    monkeypatch.setattr(
        bootstrap_module,
        "bootstrap",
        lambda: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )
    monkeypatch.setattr(connector_service, "init_otel_metrics", lambda: None)
    app = _app_with_bundled_economics()

    with pytest.raises(RuntimeError, match="Hosted economic plane failed to initialize"):
        await service_lifespan(app).__aenter__()

    assert app.state.regulus_registration_ready is False


@pytest.mark.asyncio
async def test_parent_lifespan_validates_hosted_settings_before_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_hosted_mode(monkeypatch)
    monkeypatch.setattr(config.settings, "workos_authkit_enabled", True)
    calls: list[str] = []

    def refuse_invalid_settings() -> None:
        calls.append("validate")
        raise config.EconConfigError("missing hosted credential")

    monkeypatch.setattr(config, "validate_startup_settings", refuse_invalid_settings)
    monkeypatch.setattr(bootstrap_module, "bootstrap", lambda: calls.append("bootstrap"))
    monkeypatch.setattr(connector_service, "init_otel_metrics", lambda: None)
    app = _app_with_bundled_economics()

    with pytest.raises(RuntimeError, match="Hosted economic plane failed to initialize"):
        await service_lifespan(app).__aenter__()

    assert calls == ["validate"]
    assert app.state.regulus_registration_ready is False
