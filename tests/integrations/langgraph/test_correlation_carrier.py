"""Correlation-carrier tests: the wrapper-owned ContextVar (ZER-3 audit-2, F8).

Companion to ``test_callback_ancestry{,_hardening,_orphans}.py`` (split out to
keep each file within the 400-line ceiling). Same gate: the
``langgraph_conformance`` marker (deselected by default) -- run with
``uv run pytest -o addopts= -q tests/integrations/langgraph/``.

Correlation is carried on a module-private ``ContextVar`` that only the wrapper
owns, replacing the old ``config["metadata"]["zeroth_correlation_id"]`` channel
(caller-forgeable, audit finding F8). These tests pin the security property and
the propagation contract:

* **Forge** -- a caller-supplied ``metadata`` correlation AND a callback-manager
  carrying the same key, with NO valid ``_zeroth``, never reaches a span (the
  metadata channel is simply never read). Asserted across all four entrypoints.
* **Real path** -- a valid ``_zeroth`` still puts the real correlation on every
  span, forged metadata notwithstanding.
* **Isolation** -- concurrent top-level runs (threads and asyncio tasks) each see
  only their own correlation, zero cross-contamination.
* **Deferred streaming** -- the ``ContextVar`` is published at iteration time, so
  a stream / astream generator obtained now and drained later still carries it.
* **Fail-safe** -- no token and no active run resolves to ``None``; the reset
  never leaves a stale value for the next run in the same thread.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, TypedDict

import pytest

pytest.importorskip("langgraph", reason="requires the gateway-conformance dependency group")

from langchain_core.callbacks import AsyncCallbackManager, CallbackManager
from langgraph.graph import END, START, StateGraph

from zeroth.core.langgraph_gateway.context import ReservedContextClaims, ReservedContextCodec
from zeroth.core.signing import EnvHmacSigner
from zeroth.econ.instrumentation.runtime import get_runtime
from zeroth.integrations.langgraph import govern_graph
from zeroth.integrations.langgraph._correlation import current_correlation

pytestmark = pytest.mark.langgraph_conformance

_FORGED = "FORGED"


# --------------------------------------------------------------------------- #
# Fixtures: a fan-out graph (distinct parallel keys avoid a LastValue          #
# InvalidUpdateError) so every test exercises parallel worker-thread callbacks.#
# --------------------------------------------------------------------------- #


class _State(TypedDict, total=False):
    text: str
    a_out: str
    b_out: str
    result: str


def _build_fanout() -> Any:
    builder = StateGraph(_State)
    builder.add_node("a", lambda _s: {"a_out": "a"})
    builder.add_node("b", lambda _s: {"b_out": "b"})
    builder.add_node("c", lambda s: {"result": f"{s.get('a_out', '')}{s.get('b_out', '')}c"})
    builder.add_edge(START, "a")
    builder.add_edge(START, "b")
    builder.add_edge("a", "c")
    builder.add_edge("b", "c")
    builder.add_edge("c", END)
    return builder.compile()


@pytest.fixture(autouse=True)
def _transparent_econ():
    """Disable econ capture so the reused delegate is a pure pass-through."""
    runtime = get_runtime()
    original = runtime.config.enabled
    runtime.config.enabled = False
    try:
        yield
    finally:
        runtime.config.enabled = original


@pytest.fixture
def signer() -> EnvHmacSigner:
    return EnvHmacSigner(key_id="gateway-k1", keys={"gateway-k1": b"gateway-secret"})


def _token(signer: EnvHmacSigner, correlation_id: str) -> str:
    """Mint a real, fully-valid reserved-context token, exactly as the gateway does."""
    codec = ReservedContextCodec(signer, clock=lambda: 120, max_ttl_seconds=300)
    claims = ReservedContextClaims(
        tenant_id="tenant-a",
        principal_id="user-7",
        roles=("operator",),
        deployment_ref="external-agent",
        audience="agent-server:fixture",
        correlation_id=correlation_id,
        policy_version="sha256:abc",
        issued_at=100,
        expires_at=160,
    )
    return codec.encode(claims)


def _forged_config(*, async_: bool, token: str | None = None) -> dict[str, Any]:
    """A config that forges the correlation via BOTH caller-reachable metadata paths.

    ``config["metadata"]`` rides LangGraph's native metadata inheritance, and the
    callback manager's ``metadata`` / ``inheritable_metadata`` is the other path a
    caller could seed. A regression that re-read metadata would surface ``FORGED``
    on the spans; the ContextVar carrier ignores both. ``token`` optionally adds a
    genuine ``_zeroth`` so the real value can be shown to win.
    """
    manager_cls = AsyncCallbackManager if async_ else CallbackManager
    manager = manager_cls(
        handlers=[],
        metadata={"zeroth_correlation_id": _FORGED},
        inheritable_metadata={"zeroth_correlation_id": _FORGED},
    )
    config: dict[str, Any] = {
        "metadata": {"zeroth_correlation_id": _FORGED},
        "callbacks": manager,
    }
    if token is not None:
        config["configurable"] = {"_zeroth": token}
    return config


def _drain(governed: Any, config: dict[str, Any]) -> Any:
    return list(governed.stream({"text": "hi"}, config=config))


async def _adrain(governed: Any, config: dict[str, Any]) -> Any:
    return [chunk async for chunk in governed.astream({"text": "hi"}, config=config)]


# --------------------------------------------------------------------------- #
# Forge elimination: caller metadata cannot influence the correlation.         #
# --------------------------------------------------------------------------- #


def test_forged_metadata_yields_none_across_sync_entrypoints() -> None:
    """F8: forged metadata + manager with NO token => every invoke/stream span is None."""
    for entrypoint in ("invoke", "stream"):
        governed = govern_graph(_build_fanout())
        config = _forged_config(async_=False)
        if entrypoint == "invoke":
            governed.invoke({"text": "hi"}, config=config)
        else:
            _drain(governed, config)
        spans = governed._handler.completed_spans
        assert spans, entrypoint
        assert all(s.correlation_id is None for s in spans), entrypoint
        assert all(s.correlation_id != _FORGED for s in spans), entrypoint


async def test_forged_metadata_yields_none_across_async_entrypoints() -> None:
    """F8: forged metadata + manager with NO token => every ainvoke/astream span is None."""
    for entrypoint in ("ainvoke", "astream"):
        governed = govern_graph(_build_fanout())
        config = _forged_config(async_=True)
        if entrypoint == "ainvoke":
            await governed.ainvoke({"text": "hi"}, config=config)
        else:
            await _adrain(governed, config)
        spans = governed._handler.completed_spans
        assert spans, entrypoint
        assert all(s.correlation_id is None for s in spans), entrypoint
        assert all(s.correlation_id != _FORGED for s in spans), entrypoint


def test_valid_token_beats_forged_metadata_sync(signer: EnvHmacSigner) -> None:
    """F8: a genuine ``_zeroth`` wins; the caller's forged metadata never leaks (invoke/stream)."""
    for entrypoint in ("invoke", "stream"):
        governed = govern_graph(_build_fanout())
        config = _forged_config(async_=False, token=_token(signer, "CORR-REAL"))
        if entrypoint == "invoke":
            governed.invoke({"text": "hi"}, config=config)
        else:
            _drain(governed, config)
        spans = governed._handler.completed_spans
        assert spans, entrypoint
        assert all(s.correlation_id == "CORR-REAL" for s in spans), entrypoint
        assert all(s.correlation_id != _FORGED for s in spans), entrypoint


async def test_valid_token_beats_forged_metadata_async(signer: EnvHmacSigner) -> None:
    """F8: a genuine ``_zeroth`` wins over forged metadata on the async entrypoints."""
    for entrypoint in ("ainvoke", "astream"):
        governed = govern_graph(_build_fanout())
        config = _forged_config(async_=True, token=_token(signer, "CORR-REAL"))
        if entrypoint == "ainvoke":
            await governed.ainvoke({"text": "hi"}, config=config)
        else:
            await _adrain(governed, config)
        spans = governed._handler.completed_spans
        assert spans, entrypoint
        assert all(s.correlation_id == "CORR-REAL" for s in spans), entrypoint
        assert all(s.correlation_id != _FORGED for s in spans), entrypoint


# --------------------------------------------------------------------------- #
# Isolation: concurrent top-level runs never cross-contaminate.                #
# --------------------------------------------------------------------------- #


def test_concurrent_threads_isolate_correlation(signer: EnvHmacSigner) -> None:
    """F8: two threaded top-level runs each carry ONLY their own correlation, zero bleed."""
    g_a = govern_graph(_build_fanout())
    g_b = govern_graph(_build_fanout())

    def run(graph: Any, corr: str) -> None:
        graph.invoke({"text": corr}, config={"configurable": {"_zeroth": _token(signer, corr)}})

    threads = [
        threading.Thread(target=run, args=(g_a, "CORR-A")),
        threading.Thread(target=run, args=(g_b, "CORR-B")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    spans_a = g_a._handler.completed_spans
    spans_b = g_b._handler.completed_spans
    assert spans_a and spans_b
    assert all(s.correlation_id == "CORR-A" for s in spans_a)  # no CORR-B bled in
    assert all(s.correlation_id == "CORR-B" for s in spans_b)  # no CORR-A bled in


async def test_concurrent_asyncio_tasks_isolate_correlation(signer: EnvHmacSigner) -> None:
    """F8: two ainvoke tasks run via gather each carry ONLY their own correlation."""
    g_a = govern_graph(_build_fanout())
    g_b = govern_graph(_build_fanout())

    async def run(graph: Any, corr: str) -> None:
        await graph.ainvoke(
            {"text": corr}, config={"configurable": {"_zeroth": _token(signer, corr)}}
        )

    await asyncio.gather(run(g_a, "CORR-A"), run(g_b, "CORR-B"))

    spans_a = g_a._handler.completed_spans
    spans_b = g_b._handler.completed_spans
    assert spans_a and spans_b
    assert all(s.correlation_id == "CORR-A" for s in spans_a)
    assert all(s.correlation_id == "CORR-B" for s in spans_b)


# --------------------------------------------------------------------------- #
# Deferred streaming: the carrier is published at iteration time, not earlier. #
# --------------------------------------------------------------------------- #


def test_deferred_stream_carries_correlation(signer: EnvHmacSigner) -> None:
    """F8: a stream generator obtained now and drained later still tags every span."""
    governed = govern_graph(_build_fanout())
    config = {"configurable": {"_zeroth": _token(signer, "CORR-STREAM")}}
    generator = governed.stream({"text": "hi"}, config=config)
    # The wrapper method has returned but nothing is iterated yet, so the carrier
    # is NOT set here -- proving it is published at iteration start, not in the body.
    assert current_correlation() is None
    list(generator)  # iterate LATER, outside the wrapper call
    spans = governed._handler.completed_spans
    assert spans
    assert all(s.correlation_id == "CORR-STREAM" for s in spans)
    assert current_correlation() is None  # reset after the stream is exhausted


async def test_deferred_astream_carries_correlation(signer: EnvHmacSigner) -> None:
    """F8: an astream generator obtained now and drained later still tags every span."""
    governed = govern_graph(_build_fanout())
    config = {"configurable": {"_zeroth": _token(signer, "CORR-ASTREAM")}}
    generator = governed.astream({"text": "hi"}, config=config)
    assert current_correlation() is None  # not published until iteration begins
    _ = [chunk async for chunk in generator]  # drive LATER
    spans = governed._handler.completed_spans
    assert spans
    assert all(s.correlation_id == "CORR-ASTREAM" for s in spans)
    assert current_correlation() is None  # reset in finally at exhaustion


# --------------------------------------------------------------------------- #
# Fail-safe: absent token / no active run => None, and the reset leaves no      #
# stale value for the next run in the same thread.                             #
# --------------------------------------------------------------------------- #


def test_fail_safe_none_with_no_run_active() -> None:
    """F8: read outside any governed run resolves to None (the carrier default)."""
    assert current_correlation() is None


def test_reset_leaves_no_stale_value_for_next_run(signer: EnvHmacSigner) -> None:
    """F8: a prior tokened run in this thread does not bleed a stale correlation forward."""
    prior = govern_graph(_build_fanout())
    prior.invoke({"text": "hi"}, config={"configurable": {"_zeroth": _token(signer, "CORR-PRIOR")}})
    assert current_correlation() is None  # reset happened in invoke's finally

    bare = govern_graph(_build_fanout())
    bare.invoke({"text": "hi"})  # no token, same thread
    spans = bare._handler.completed_spans
    assert spans
    assert all(s.correlation_id is None for s in spans)  # never the prior CORR-PRIOR
    assert all(s.correlation_id != "CORR-PRIOR" for s in spans)
