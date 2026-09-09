"""Explicit ownership for the optional decision scheduler task."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI

from zeroth.econ.plane.decisioning.scheduler import observe, run_scheduler_loop

SchedulerLoop = Callable[..., Coroutine[Any, Any, None]]


@dataclass(frozen=True)
class SchedulerHandle:
    owner: object
    stop: asyncio.Event
    task: asyncio.Task[None]


def assert_scheduler_available(app: FastAPI) -> None:
    """Reject a second lifecycle before it mutates shared mounted state."""
    if getattr(app.state, "cloud_scheduler_handle", None) is not None:
        observe("record_duplicate_owner_attempt")
        raise RuntimeError("cloud decision scheduler already has an owner")
    task = getattr(app.state, "cloud_scheduler_task", None)
    if task is not None and not task.done():
        observe("record_duplicate_owner_attempt")
        raise RuntimeError("cloud decision scheduler already has an owner")


def start_scheduler(
    app: FastAPI,
    *,
    owner: object,
    enabled: bool,
    interval_seconds: float,
    run_loop: SchedulerLoop = run_scheduler_loop,
) -> SchedulerHandle | None:
    """Start one explicitly enabled scheduler and return its ownership token."""
    if not enabled:
        return None
    assert_scheduler_available(app)
    stop = asyncio.Event()
    task = asyncio.create_task(
        run_loop(stop, interval_seconds=interval_seconds),
        name="zeroth-cloud-decision-scheduler",
    )
    handle = SchedulerHandle(owner=owner, stop=stop, task=task)
    app.state.cloud_scheduler_handle = handle
    app.state.cloud_scheduler_stop = stop
    app.state.cloud_scheduler_task = task
    observe("set_active_owner", True)
    return handle


async def stop_scheduler(app: FastAPI, handle: SchedulerHandle | None) -> None:
    """Drain the task owned by *handle*; never stop another lifecycle's task."""
    if handle is None:
        return
    if getattr(app.state, "cloud_scheduler_handle", None) is not handle:
        raise RuntimeError("cloud decision scheduler handle is not the active owner")
    handle.stop.set()
    cancelled = False
    try:
        while not handle.task.done():
            try:
                await asyncio.shield(handle.task)
            except asyncio.CancelledError:
                # The thread behind asyncio.to_thread cannot be cancelled. Keep
                # ownership and drain it before propagating caller cancellation.
                cancelled = True
        handle.task.result()
    finally:
        if handle.task.done() and getattr(app.state, "cloud_scheduler_handle", None) is handle:
            app.state.cloud_scheduler_handle = None
            observe("set_active_owner", False)
    if cancelled:
        raise asyncio.CancelledError


__all__ = ["SchedulerHandle", "assert_scheduler_available", "start_scheduler", "stop_scheduler"]
