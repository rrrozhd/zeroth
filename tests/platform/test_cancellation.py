"""The one place cancellation is held back while cleanup finishes."""

from __future__ import annotations

import asyncio
import tempfile
import threading
from pathlib import Path

import pytest

from zeroth.platform.primitives import (
    cleanup_despite_cancellation,
    finish_despite_cancellation,
    finish_in_thread,
)


async def _blocking(release: asyncio.Event, finished: asyncio.Event, value: object = "done"):
    await release.wait()
    finished.set()
    return value


@pytest.mark.asyncio
async def test_undisturbed_work_returns_its_result_and_no_cancellations() -> None:
    async def work():
        await asyncio.sleep(0)
        return 42

    assert await finish_despite_cancellation(work()) == (42, 0)


@pytest.mark.asyncio
async def test_repeated_cancellation_is_held_back_and_counted() -> None:
    release, finished = asyncio.Event(), asyncio.Event()
    signals = 0

    def on_cancel() -> None:
        nonlocal signals
        signals += 1

    task = asyncio.create_task(
        finish_despite_cancellation(_blocking(release, finished), on_cancel=on_cancel)
    )
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    assert not finished.is_set()
    release.set()
    assert await task == ("done", 2)
    assert finished.is_set()
    assert signals == 2


@pytest.mark.asyncio
async def test_work_exception_surfaces_unchanged_after_a_cancellation() -> None:
    release, finished = asyncio.Event(), asyncio.Event()
    error = ValueError("cleanup failed")

    async def failing():
        await _blocking(release, finished)
        raise error

    task = asyncio.create_task(finish_despite_cancellation(failing()))
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    with pytest.raises(ValueError) as caught:
        await task
    assert caught.value is error
    assert finished.is_set()


@pytest.mark.asyncio
async def test_cleanup_helper_reraises_cancellation_only_after_completion() -> None:
    release, finished = asyncio.Event(), asyncio.Event()

    task = asyncio.create_task(cleanup_despite_cancellation(_blocking(release, finished)))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()

    quiet = asyncio.Event()
    quiet.set()
    assert await cleanup_despite_cancellation(_blocking(quiet, asyncio.Event())) is None


@pytest.mark.asyncio
async def test_an_expired_timeout_is_still_recognised_as_a_timeout() -> None:
    """Holding the cancellation back must not disturb the requester's accounting."""
    finished = asyncio.Event()

    async def slow_cleanup():
        await asyncio.sleep(0.05)
        finished.set()

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            await cleanup_despite_cancellation(slow_cleanup())
    assert finished.is_set()


def test_loop_shutdown_waits_for_thread_work_before_the_awaiter_unwinds() -> None:
    """asyncio.run cancels every task at shutdown; a running thread must still finish first."""
    entered = threading.Event()
    release = threading.Event()
    observed: dict[str, object] = {}

    def write_into(root: Path) -> None:
        entered.set()
        release.wait(2)
        observed["root_present_at_write"] = root.exists()
        (root / "out.json").write_text("{}")

    async def awaiter() -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            observed["root"] = root
            await finish_in_thread(write_into, root)

    async def main() -> None:
        asyncio.create_task(awaiter())
        await asyncio.to_thread(entered.wait, 2)
        asyncio.get_running_loop().call_later(0.1, release.set)
        # Return with the awaiter pending: shutdown cancels it while the thread runs.

    asyncio.run(main())

    assert observed["root_present_at_write"] is True
    assert not observed["root"].exists()


@pytest.mark.asyncio
async def test_thread_work_propagates_context_and_holds_repeated_cancellation() -> None:
    import contextvars

    marker: contextvars.ContextVar[str] = contextvars.ContextVar("marker", default="unset")
    marker.set("propagated")
    release = threading.Event()

    def work(value: int) -> tuple[str, int]:
        release.wait(2)
        return marker.get(), value

    task = asyncio.create_task(finish_in_thread(work, 7))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    assert await task == (("propagated", 7), 2)
