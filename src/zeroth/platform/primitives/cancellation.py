"""Finish in-flight work when the awaiting task is cancelled.

Cleanup that must run to completion -- a transaction ``__aexit__``, removal of
a container this process owns, a worker thread that still holds a workspace --
cannot simply be awaited: the first ``CancelledError`` delivered to the awaiting
task would abandon it half-done. The helper below holds every cancellation back
until the work has finished, then reports it to the caller, which decides
whether to swallow it or re-raise it. It exists so the loop is written once:
each hand-written copy had to get the same three details right (``shield``
re-armed after every cancellation, the task's cancellation count left intact so
an enclosing ``asyncio.timeout`` still recognises its own expiry, and the work's
own exception surfacing unchanged).
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
from collections.abc import Awaitable, Callable


async def finish_despite_cancellation[T](
    work: Awaitable[T],
    *,
    on_cancel: Callable[[], None] | None = None,
) -> tuple[T, int]:
    """Await ``work`` to completion, holding back cancellation of the current task.

    Returns the result together with the number of cancellation requests that
    arrived meanwhile. ``on_cancel`` runs once per request, for example to ask
    the work to stop cooperatively. Exceptions raised by ``work`` propagate
    unchanged once it has finished. The task's cancellation count is not reset:
    a caller that re-raises ``asyncio.CancelledError`` leaves the accounting as
    the requester expects, and a caller that swallows the cancellation instead
    must call ``asyncio.current_task().uncancel()`` once per reported request.
    """
    task = asyncio.ensure_future(work)
    cancellations = 0
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancellations += 1
            if on_cancel is not None:
                on_cancel()
    return task.result(), cancellations


async def cleanup_despite_cancellation(work: Awaitable[object]) -> None:
    """Run ``work`` to completion, then re-raise any cancellation that arrived."""
    _, cancellations = await finish_despite_cancellation(work)
    if cancellations:
        raise asyncio.CancelledError


async def finish_in_thread[T](
    fn: Callable[..., T],
    /,
    *args: object,
    on_cancel: Callable[[], None] | None = None,
    **kwargs: object,
) -> tuple[T, int]:
    """Run ``fn`` in the default executor and hold cancellation until it returns.

    A thread cannot be interrupted, so the awaiter must not unwind while it is
    still running -- the caller's workspace, connection or file would be torn
    down under it. ``asyncio.to_thread`` wrapped in a task is not enough:
    ``asyncio.run`` cancels every task at shutdown, and a cancelled task around
    a running thread lets its awaiter proceed immediately. The future returned
    by ``run_in_executor`` is not a task, so shutdown leaves it alone and the
    shield loop keeps waiting until the thread has actually finished. Context
    variables are propagated like ``asyncio.to_thread`` does.
    """
    loop = asyncio.get_running_loop()
    context = contextvars.copy_context()
    future = loop.run_in_executor(None, functools.partial(context.run, fn, *args, **kwargs))
    return await finish_despite_cancellation(future, on_cancel=on_cancel)
