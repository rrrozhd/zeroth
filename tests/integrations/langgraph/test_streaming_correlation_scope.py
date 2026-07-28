"""ZER-3 audit-3 (F10/F11): per-chunk correlation scoping + delegate close propagation.

These exercise the governed streaming wrappers against a duck-typed fake graph, so
they need no ``langgraph`` and run in the default suite. Econ capture is disabled so
the governed generator wraps the fake's generator directly and the assertions isolate
the wrapper's own scoping/cleanup behaviour.
"""

from __future__ import annotations

import base64
import json

import pytest

from zeroth.integrations.langgraph import govern_graph
from zeroth.integrations.langgraph._correlation import current_correlation

CORRELATION = "corr-stream-scope"


def _zeroth_token(correlation_id: str) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"correlation_id": correlation_id}).encode()
    ).decode()
    return f"header.{payload}.signature"


def _config() -> dict:
    return {"configurable": {"_zeroth": _zeroth_token(CORRELATION)}}


class _FakeGraph:
    """Records the correlation visible while it advances, and whether it was closed."""

    def __init__(self) -> None:
        self.seen: list[str | None] = []
        self.stream_closed = False
        self.astream_closed = False

    def stream(self, *args, **kwargs):
        try:
            self.seen.append(current_correlation())
            yield {"n": 1}
            self.seen.append(current_correlation())
            yield {"n": 2}
        finally:
            self.stream_closed = True

    async def astream(self, *args, **kwargs):
        try:
            self.seen.append(current_correlation())
            yield {"n": 1}
            self.seen.append(current_correlation())
            yield {"n": 2}
        finally:
            self.astream_closed = True


@pytest.fixture(autouse=True)
def _econ_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass econ capture so the governed generator wraps the fake directly."""
    monkeypatch.setattr(
        "zeroth.econ.instrumentation.integrations.langgraph.should_capture_layer",
        lambda _layer: False,
    )


def test_f10_stream_correlation_is_not_visible_to_the_consumer() -> None:
    fake = _FakeGraph()
    governed = govern_graph(fake)

    chunks = []
    for chunk in governed.stream({}, config=_config()):
        # Reset happens BEFORE the yield: the consumer never sees the correlation.
        assert current_correlation() is None
        chunks.append(chunk)

    assert chunks == [{"n": 1}, {"n": 2}]
    # Non-vacuous: the delegate DID observe it while advancing.
    assert fake.seen == [CORRELATION, CORRELATION]
    assert current_correlation() is None


async def test_f10_astream_correlation_is_not_visible_to_the_consumer() -> None:
    fake = _FakeGraph()
    governed = govern_graph(fake)

    chunks = []
    async for chunk in governed.astream({}, config=_config()):
        assert current_correlation() is None
        chunks.append(chunk)

    assert chunks == [{"n": 1}, {"n": 2}]
    assert fake.seen == [CORRELATION, CORRELATION]
    assert current_correlation() is None


def test_f10_abandoned_stream_leaks_no_correlation() -> None:
    fake = _FakeGraph()
    stream = govern_graph(fake).stream({}, config=_config())

    assert next(stream) == {"n": 1}
    stream.close()

    assert current_correlation() is None
    assert fake.stream_closed is True


def test_f11_closing_astream_closes_the_delegate() -> None:
    """The wrapper must explicitly aclose the delegate it iterates.

    Asserted against the wrapper's *immediate* delegate: `async for` does not close
    its iterator on early exit, so each layer must propagate the close itself. (The
    econ layer normally sitting here does not yet propagate further down — a
    pre-existing gap tracked separately, out of this fix's scope.)
    """
    import asyncio

    async def scenario() -> None:
        governed = govern_graph(_FakeGraph())
        delegate = _FakeGraph()
        governed._delegate = delegate  # this wrapper's immediate delegate

        stream = governed.astream({}, config=_config())
        assert await stream.__anext__() == {"n": 1}
        await stream.aclose()
        # Asserted INSIDE the loop, immediately after aclose: the loop's
        # shutdown_asyncgens would otherwise finalise it later and mask a wrapper
        # that never propagated the close.
        assert delegate.astream_closed is True, "delegate finally must run at aclose()"
        assert current_correlation() is None

    asyncio.run(scenario())


def test_f11_closing_stream_closes_the_delegate() -> None:
    fake = _FakeGraph()
    stream = govern_graph(fake).stream({}, config=_config())

    assert next(stream) == {"n": 1}
    stream.close()

    assert fake.stream_closed is True
