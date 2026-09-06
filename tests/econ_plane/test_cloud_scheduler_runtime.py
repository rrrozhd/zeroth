from __future__ import annotations

import asyncio

import pytest

from zeroth.platform.storage.schema_revision import SchemaRevision


@pytest.mark.asyncio
async def test_scheduler_runs_due_scans_and_stops_without_waiting_for_next_interval() -> None:
    from zeroth.econ.plane.decisioning.scheduler import run_scheduler_loop

    stop = asyncio.Event()
    calls = 0

    def run_once() -> int:
        nonlocal calls
        calls += 1
        stop.set()
        return 2

    await run_scheduler_loop(stop, interval_seconds=60, run_once=run_once)

    assert calls == 1


@pytest.mark.asyncio
async def test_scheduler_survives_one_scan_failure_and_retries() -> None:
    from zeroth.econ.plane.decisioning.scheduler import run_scheduler_loop

    stop = asyncio.Event()
    calls = 0

    def run_once() -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("database temporarily unavailable")
        stop.set()
        return 0

    await run_scheduler_loop(stop, interval_seconds=0.001, run_once=run_once)

    assert calls == 2


def test_plane_lifecycle_runs_scheduler_and_exposes_it_in_readiness(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from zeroth.econ.plane import main
    from zeroth.econ.plane.config import settings

    async def loop(stop: asyncio.Event, *, interval_seconds: float) -> None:
        assert interval_seconds == 7
        await stop.wait()

    monkeypatch.setattr(settings, "jwt_secret", "configured-secret")
    monkeypatch.setattr(settings, "cloud_entitlements_enabled", True)
    monkeypatch.setattr(settings, "cloud_scheduler_enabled", True)
    monkeypatch.setattr(settings, "cloud_scheduler_interval_seconds", 7)
    monkeypatch.setattr(settings, "workos_authkit_enabled", False)
    monkeypatch.setattr(settings, "paddle_billing_enabled", False)
    monkeypatch.setattr(main.common_bootstrap, "bootstrap", lambda: None)
    monkeypatch.setattr(
        main.common_bootstrap,
        "schema_revision",
        lambda: SchemaRevision(applied="head", head="head", state="current"),
    )
    monkeypatch.setattr(main, "init_otel_metrics", lambda: None)
    monkeypatch.setattr(main, "run_scheduler_loop", loop)

    with TestClient(main.app) as client:
        response = client.get("/health/ready")
        task = main.app.state.cloud_scheduler_task

        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert response.json()["scheduler"] == {"status": "ok"}
        assert not task.done()

    assert task.done()


@pytest.mark.parametrize("path", ["/health", "/health/ready"])
@pytest.mark.parametrize("schema_state", ["current", "behind", "unknown"])
@pytest.mark.parametrize("scheduler_state", ["disabled", "running", "missing", "finished", "cancelled"])
def test_health_http_status_matches_required_dependency_state(
    monkeypatch, path, schema_state, scheduler_state
) -> None:
    from concurrent.futures import Future

    from fastapi.testclient import TestClient

    from zeroth.econ.plane import main
    from zeroth.econ.plane.config import settings

    task = Future()
    if scheduler_state == "finished":
        task.set_result(None)
    elif scheduler_state == "cancelled":
        task.cancel()
    monkeypatch.setattr(settings, "cloud_scheduler_enabled", scheduler_state != "disabled")
    monkeypatch.setattr(
        main.app.state,
        "cloud_scheduler_task",
        None if scheduler_state == "missing" else task,
        raising=False,
    )
    revision = SchemaRevision(applied="old", head="head", state=schema_state)
    monkeypatch.setattr(main.common_bootstrap, "schema_revision", lambda: revision)

    # No lifespan: inspect each dependency state without starting a replacement task.
    response = TestClient(main.app).get(path)
    ready = schema_state == "current" and scheduler_state in {"disabled", "running"}
    assert response.json()["status"] == ("ok" if ready else "degraded")
    assert response.status_code == (200 if ready else 503)
    assert response.json()["schema_revision"] == revision.model_dump()
