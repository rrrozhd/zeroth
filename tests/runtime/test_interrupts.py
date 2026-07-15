"""Regression tests for the governed interrupt manager.

Guards the v0.10.0.0.2 fix: ``InterruptManager.resolve()`` must raise a
locally-defined ``InterruptExpiredError`` when an interrupt has expired. The
v0.10 ``governai`` absorption left ``resolve()`` importing that exception from
the never-vendored ``zeroth.core.governed.workflows`` package, so the path
raised ``ModuleNotFoundError`` — and a naive re-add would instead raise
``TypeError``, since the raise site passes ``request=``. ``InterruptManager``
has no production callers, so without these tests the regression re-breaks
silently.
"""

from __future__ import annotations

import time

import pytest

from zeroth.core.governed.runtime.interrupts import (
    InMemoryInterruptStore,
    InterruptExpiredError,
    InterruptManager,
    InterruptRequest,
)


def test_interrupt_expired_error_shape() -> None:
    """The restored exception subclasses RuntimeError and carries the request."""
    err = InterruptExpiredError("expired", request=None)
    assert isinstance(err, RuntimeError)
    assert err.request is None
    assert str(err) == "expired"


@pytest.mark.asyncio
async def test_resolve_expired_interrupt_raises_interrupt_expired_error() -> None:
    """resolve() on an expired interrupt raises InterruptExpiredError with the request.

    This is the core regression guard: the exception must resolve locally (no
    ModuleNotFoundError) and accept ``request=`` (no TypeError).
    """
    store = InMemoryInterruptStore()
    manager = InterruptManager(store=store)
    now = int(time.time())
    expired = InterruptRequest(
        interrupt_id="i1",
        run_id="r1",
        step_name="step",
        message="waiting",
        created_at=now - 100,
        expires_at=now - 10,
        status="pending",
    )
    await store.save_request(expired)

    with pytest.raises(InterruptExpiredError) as exc_info:
        await manager.resolve(run_id="r1", interrupt_id="i1", response="ok")

    assert exc_info.value.request is not None
    assert exc_info.value.request.interrupt_id == "i1"
    # resolve() marks the request expired as a side effect.
    stored = await store.get_request("r1", "i1")
    assert stored is not None
    assert stored.status == "expired"


@pytest.mark.asyncio
async def test_resolve_live_interrupt_succeeds() -> None:
    """A live pending interrupt resolves and is marked resolved."""
    manager = InterruptManager()
    request = await manager.create(run_id="r1", step_name="step", message="waiting")

    resolution = await manager.resolve(
        run_id="r1", interrupt_id=request.interrupt_id, response={"answer": 42}
    )

    assert resolution.request.interrupt_id == request.interrupt_id
    assert resolution.response == {"answer": 42}
    assert resolution.request.status == "resolved"


@pytest.mark.asyncio
async def test_resolve_unknown_interrupt_raises_keyerror() -> None:
    """An unknown interrupt id raises KeyError, distinct from expiry."""
    manager = InterruptManager()
    with pytest.raises(KeyError):
        await manager.resolve(run_id="r1", interrupt_id="missing", response="ok")


@pytest.mark.asyncio
async def test_resolve_epoch_mismatch_raises_valueerror() -> None:
    """A stale epoch is rejected with ValueError before resolution."""
    manager = InterruptManager()
    request = await manager.create(run_id="r1", step_name="step", message="waiting")
    with pytest.raises(ValueError):
        await manager.resolve(
            run_id="r1",
            interrupt_id=request.interrupt_id,
            response="ok",
            epoch=request.epoch + 1,
        )
