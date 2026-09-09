import asyncio
import threading

from fastapi import FastAPI
import pytest

from zeroth.econ.plane import config, main
from zeroth.econ.plane.decisioning.scheduler import run_scheduler_loop


@pytest.mark.asyncio
async def test_duplicate_standalone_start_is_rejected_without_orphan(monkeypatch):
    app = FastAPI()
    monkeypatch.setattr(main, "app", app)
    monkeypatch.setattr(config.settings, "cloud_scheduler_enabled", True)

    async def loop(stop, *, interval_seconds):
        await stop.wait()

    monkeypatch.setattr(main, "run_scheduler_loop", loop)
    await main.start_cloud_scheduler()
    first, first_stop = app.state.cloud_scheduler_task, app.state.cloud_scheduler_stop
    try:
        with pytest.raises(RuntimeError, match="owner"):
            await main.start_cloud_scheduler()
        assert app.state.cloud_scheduler_task is first
        assert not first.done()
    finally:
        first_stop.set()
        await main.stop_cloud_scheduler()
        await first


@pytest.mark.asyncio
async def test_shutdown_waits_for_inflight_thread_pass(monkeypatch):
    app = FastAPI()
    monkeypatch.setattr(main, "app", app)
    monkeypatch.setattr(config.settings, "cloud_scheduler_enabled", True)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def scan():
        started.set()
        assert release.wait(3), "test must release its local scan"
        finished.set()
        return 0

    async def loop(stop, *, interval_seconds):
        await run_scheduler_loop(stop, interval_seconds=60, run_once=scan)

    monkeypatch.setattr(main, "run_scheduler_loop", loop)
    await main.start_cloud_scheduler()
    task = app.state.cloud_scheduler_task
    stopper = None
    try:
        assert await asyncio.to_thread(started.wait, 1)
        stopper = asyncio.create_task(main.stop_cloud_scheduler())
        await asyncio.sleep(0)
        assert not stopper.done()
        assert not finished.is_set()
    finally:
        release.set()
        if stopper is not None:
            await stopper
        else:
            await main.stop_cloud_scheduler()
    assert finished.is_set()
    assert task.done()


@pytest.mark.asyncio
async def test_cancelled_shutdown_still_drains_inflight_thread_pass(monkeypatch):
    app = FastAPI()
    monkeypatch.setattr(main, "app", app)
    monkeypatch.setattr(config.settings, "cloud_scheduler_enabled", True)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def scan():
        started.set()
        assert release.wait(3)
        finished.set()
        return 0

    async def loop(stop, *, interval_seconds):
        await run_scheduler_loop(stop, interval_seconds=60, run_once=scan)

    monkeypatch.setattr(main, "run_scheduler_loop", loop)
    await main.start_cloud_scheduler()
    stopper = asyncio.create_task(main.stop_cloud_scheduler())
    assert await asyncio.to_thread(started.wait, 1)
    stopper.cancel()
    await asyncio.sleep(0)
    assert not finished.is_set()
    assert app.state.cloud_scheduler_handle is not None
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await stopper
    assert finished.is_set()
    assert app.state.cloud_scheduler_handle is None
    assert app.state.cloud_scheduler_standalone_handle is None
    await main.stop_cloud_scheduler()
