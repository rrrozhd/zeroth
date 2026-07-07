"""Tests for the optional Zeroth Console static mount and its cross-origin path.

Covers the four constraints that make 'one bundle, both modes' work:
  - /console static assets are served WITHOUT an API key (auth bypass),
  - the bare /console path redirects to /console/ (StaticFiles index),
  - protected /v1 routes still require auth,
  - CORS preflight (OPTIONS) is answered and bypasses auth (standalone mode).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from zeroth.core.service.auth import AuthenticationError
from zeroth.core.service.console_ui import (
    console_cors_origins,
    find_console_dir,
    mount_console,
)

API_KEY = "secret"


class _FakeAuth:
    """Authenticator that requires X-API-Key: secret, else raises."""

    def authenticate_headers(self, headers):  # noqa: ANN001 - mirrors real signature
        if headers.get("X-API-Key") != API_KEY:
            raise AuthenticationError("missing or invalid API key")
        return MagicMock()


def _make_bootstrap() -> MagicMock:
    b = MagicMock()
    b.authenticator = _FakeAuth()
    b.audit_repository = None  # record_service_denial no-ops on None
    b.regulus_client = None  # skip cost-API regulus wiring
    b.deployment.deployment_ref = "demo"
    b.deployment.version = 1
    b.deployment.graph_version_ref = "demo@1"
    return b


@pytest.fixture
def console_dir(tmp_path: Path) -> Path:
    out = tmp_path / "out"
    out.mkdir()
    (out / "index.html").write_text(
        "<!doctype html><title>Zeroth Console</title><h1>console-ok</h1>"
    )
    return out


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def test_find_console_dir_env_override(monkeypatch, console_dir: Path) -> None:
    monkeypatch.setenv("ZEROTH_CONSOLE_DIR", str(console_dir))
    assert find_console_dir() == console_dir


def test_find_console_dir_missing_returns_none(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ZEROTH_CONSOLE_DIR", str(tmp_path / "absent"))
    assert find_console_dir() is None


def test_find_console_dir_falls_back_to_installed_package(
    monkeypatch, tmp_path: Path, console_dir: Path
) -> None:
    """With no env override and no repo build, the zeroth-console package wins."""
    import sys
    from types import SimpleNamespace

    import zeroth.core.service.console_ui as console_ui

    monkeypatch.delenv("ZEROTH_CONSOLE_DIR", raising=False)
    # Point the module's repo-root detection at an empty tree so the
    # source-checkout candidate (frontend/out) can't resolve.
    monkeypatch.setattr(
        console_ui, "__file__", str(tmp_path / "a" / "b" / "c" / "d" / "console_ui.py")
    )
    monkeypatch.setitem(
        sys.modules, "zeroth_console", SimpleNamespace(console_dir=lambda: console_dir)
    )
    assert find_console_dir() == console_dir


def test_console_cors_origins_parsing(monkeypatch) -> None:
    monkeypatch.setenv("ZEROTH_CONSOLE_CORS_ORIGINS", "http://a.test, http://b.test ,")
    assert console_cors_origins() == ["http://a.test", "http://b.test"]
    monkeypatch.delenv("ZEROTH_CONSOLE_CORS_ORIGINS")
    assert console_cors_origins() == []


def test_mount_console_false_without_build(monkeypatch, tmp_path: Path) -> None:
    from fastapi import FastAPI

    monkeypatch.setenv("ZEROTH_CONSOLE_DIR", str(tmp_path / "absent"))
    assert mount_console(FastAPI()) is False


# --------------------------------------------------------------------------- #
# Integration through create_app
# --------------------------------------------------------------------------- #


@pytest.fixture
def app(monkeypatch, console_dir: Path):
    monkeypatch.setenv("ZEROTH_CONSOLE_DIR", str(console_dir))
    monkeypatch.setenv("ZEROTH_CONSOLE_CORS_ORIGINS", "http://localhost:3000")
    from zeroth.core.service.app import create_app

    return create_app(_make_bootstrap())


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


@pytest.mark.asyncio
async def test_console_index_served_without_auth(app) -> None:
    async with _client(app) as c:
        r = await c.get("/console/")  # no X-API-Key header
    assert r.status_code == 200
    assert "console-ok" in r.text


@pytest.mark.asyncio
async def test_console_bare_path_redirects(app) -> None:
    async with _client(app) as c:
        r = await c.get("/console", follow_redirects=False)
    assert r.status_code in (307, 308)
    assert r.headers["location"] == "/console/"


@pytest.mark.asyncio
async def test_protected_route_requires_auth(app) -> None:
    async with _client(app) as c:
        r = await c.get("/v1/runs/nonexistent")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_cors_preflight_bypasses_auth(app) -> None:
    async with _client(app) as c:
        r = await c.options(
            "/v1/runs",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "POST",
            },
        )
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"
