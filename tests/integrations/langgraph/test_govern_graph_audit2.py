"""Audit-hardening gate tests for the govern_graph wrapper (ZER-2, audit-2).

These cover the three findings the audit-1 suite left incomplete:

* **F4** -- chained ``with_config(...)`` must match ``RunnableBinding`` *shallow
  overwrite* of previously bound top-level keys, not merge/accumulate them.
* **F5** -- callback-manager dedup must keep exactly one governance handler in
  **both** ``handlers`` and ``inheritable_handlers`` (same identity), regardless
  of which list a pre-installed handler started in.
* **F6** -- with econ instrumentation ENABLED, a telemetry emit failure must be
  safe across ALL FOUR entrypoints (invoke / stream / ainvoke / astream), and
  the failing transport must actually be exercised (no vacuous pass).

Self-contained (own fixtures/helpers) so audit-1 stays untouched and each file
keeps well under the size budget. Gated behind the ``langgraph_conformance``
marker; run with ``-o addopts= -m langgraph_conformance``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, TypedDict

import pytest

pytest.importorskip("langgraph", reason="requires the gateway-conformance dependency group")

from langchain_core.callbacks import BaseCallbackHandler, BaseCallbackManager
from langchain_core.runnables import Runnable
from langgraph.graph import END, START, StateGraph

from zeroth.econ.instrumentation.runtime import get_runtime
from zeroth.integrations.langgraph import ZerothGovernanceCallbackHandler, govern_graph

pytestmark = pytest.mark.langgraph_conformance


class _State(TypedDict, total=False):
    mode: str
    text: str
    result: str


def _echo(state: _State) -> _State:
    return {"result": f"echo:{state.get('text', '')}"}


def _boom(_state: _State) -> _State:
    raise RuntimeError("govern-graph-fixture-error")


async def _slow(_state: _State) -> _State:
    await asyncio.sleep(30)
    return {"result": "slow-done"}


def _route(state: _State) -> str:
    return state.get("mode", "echo")


def build_graph() -> Any:
    """Compile a small, deterministic StateGraph with echo/error/cancel nodes."""
    builder = StateGraph(_State)
    builder.add_node("echo", _echo)
    builder.add_node("error", _boom)
    builder.add_node("cancel", _slow)
    builder.add_conditional_edges(
        START, _route, {"echo": "echo", "error": "error", "cancel": "cancel"}
    )
    for node in ("echo", "error", "cancel"):
        builder.add_edge(node, END)
    return builder.compile()


class _HostileEqHandler(BaseCallbackHandler):
    """User handler whose ``__eq__`` matches everything -- must not fool dedup."""

    def __eq__(self, _other: Any) -> bool:
        return True

    def __hash__(self) -> int:
        return id(self)


class _ConfigSpy(Runnable):
    """A Runnable that records the config each invoke receives, then delegates.

    Being a real ``Runnable`` lets it slot into a bare ``.with_config(...)`` chain
    for the F4 equivalence reference while also serving as the wrapped graph on
    the governed side. It records the config whether passed positionally (as a
    ``RunnableBinding`` delivers it) or by keyword (as the econ delegate does).
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.configs: list[Any] = []

    def invoke(self, payload: Any, config: Any = None, **_kwargs: Any) -> Any:
        self.configs.append(config)
        return self._inner.invoke(payload)


@pytest.fixture(autouse=True)
def _transparent_econ():
    """Disable econ capture by default; F6 tests re-enable it explicitly."""
    runtime = get_runtime()
    original = runtime.config.enabled
    runtime.config.enabled = False
    try:
        yield
    finally:
        runtime.config.enabled = original


async def _drain_async(astream: Any) -> list[Any]:
    return [chunk async for chunk in astream]


def _gov_handlers(seq: Any) -> list[Any]:
    return [h for h in seq if isinstance(h, ZerothGovernanceCallbackHandler)]


# --------------------------------------------------------------------------- F4


def _chain_configs() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Two bound layers plus a call config; each layer sets tags + metadata."""
    return (
        {"tags": ["b1"], "metadata": {"a": 1}},
        {"tags": ["b2"], "metadata": {"b": 2}},
        {"tags": ["call"], "metadata": {"c": 3}},
    )


def _observed(config: Any) -> dict[str, Any]:
    return {
        "tags": sorted(config.get("tags") or []),
        "metadata": dict(config.get("metadata") or {}),
    }


def test_f4_chained_with_config_matches_bare_shallow_overwrite() -> None:
    payload = {"mode": "echo", "text": "x"}

    b1, b2, bcall = _chain_configs()
    bare_spy = _ConfigSpy(build_graph())
    bare_result = bare_spy.with_config(b1).with_config(b2).invoke(payload, config=bcall)

    g1, g2, gcall = _chain_configs()
    gov_spy = _ConfigSpy(build_graph())
    gov_result = govern_graph(gov_spy).with_config(g1).with_config(g2).invoke(payload, config=gcall)

    # Chained with_config must SHALLOW-OVERWRITE bound top-level keys (RunnableBinding
    # semantics): "b1"/{"a":1} are replaced by "b2"/{"b":2}, never accumulated.
    assert _observed(gov_spy.configs[-1]) == _observed(bare_spy.configs[-1])
    assert gov_result == bare_result


# --------------------------------------------------------------------------- F5


@pytest.mark.parametrize(
    "in_handlers,in_inheritable",
    [
        (True, False),  # pre-installed only in handlers -> must be made inheritable
        (False, True),  # pre-installed only in inheritable -> must not duplicate
        (True, True),  # already in both
        (False, False),  # in neither
    ],
)
def test_f5_manager_governance_single_in_both_lists(
    in_handlers: bool, in_inheritable: bool
) -> None:
    spy = _ConfigSpy(build_graph())
    governed = govern_graph(spy)
    preinstalled = ZerothGovernanceCallbackHandler() if (in_handlers or in_inheritable) else None

    handlers: list[Any] = [_HostileEqHandler()]
    inheritable: list[Any] = []
    if in_handlers:
        handlers.append(preinstalled)
    if in_inheritable:
        inheritable.append(preinstalled)
    manager = BaseCallbackManager(handlers=handlers, inheritable_handlers=inheritable)

    governed.invoke({"mode": "echo", "text": "x"}, config={"callbacks": manager})

    delivered = spy.configs[-1]["callbacks"]
    assert isinstance(delivered, BaseCallbackManager)
    h_gov = _gov_handlers(delivered.handlers)
    ih_gov = _gov_handlers(delivered.inheritable_handlers)
    # Exactly one governance handler in EACH list, and the same identity in both.
    assert len(h_gov) == 1
    assert len(ih_gov) == 1
    assert h_gov[0] is ih_gov[0]
    expected = preinstalled if preinstalled is not None else governed._handler
    assert h_gov[0] is expected
    # Hostile-__eq__ user handler survives and never suppressed governance.
    assert any(isinstance(h, _HostileEqHandler) for h in delivered.handlers)


# --------------------------------------------------------------------------- F6

_ECHO = {"mode": "echo", "text": "resilient"}


def _install_failing_transport(monkeypatch: pytest.MonkeyPatch, entrypoint: str) -> list[Any]:
    """Enable econ and make the transport method for ``entrypoint`` record-then-raise."""
    runtime = get_runtime()
    runtime.config.enabled = True
    calls: list[Any] = []

    if entrypoint in ("invoke", "stream"):

        def boom(event: Any) -> None:
            calls.append(event)
            raise RuntimeError("telemetry-transport-down")

        monkeypatch.setattr(runtime.transport, "enqueue_execution", boom)
    else:

        async def aboom(event: Any) -> None:
            calls.append(event)
            raise RuntimeError("telemetry-transport-down")

        monkeypatch.setattr(runtime.transport, "aenqueue_execution", aboom)
    return calls


def _drive(governed: Any, entrypoint: str, payload: dict[str, Any]) -> Any:
    if entrypoint == "invoke":
        return governed.invoke(payload)
    if entrypoint == "stream":
        return list(governed.stream(payload))
    if entrypoint == "ainvoke":
        return asyncio.run(governed.ainvoke(payload))
    return asyncio.run(_drain_async(governed.astream(payload)))


@pytest.mark.parametrize("entrypoint", ["invoke", "stream", "ainvoke", "astream"])
def test_f6_result_survives_telemetry_failure(
    monkeypatch: pytest.MonkeyPatch, entrypoint: str
) -> None:
    governed = govern_graph(build_graph())
    calls = _install_failing_transport(monkeypatch, entrypoint)
    result = _drive(governed, entrypoint, _ECHO)
    assert "echo:resilient" in json.dumps(result, default=str)
    assert calls, "telemetry transport was never exercised"


@pytest.mark.parametrize("entrypoint", ["invoke", "stream", "ainvoke", "astream"])
def test_f6_upstream_error_not_masked_by_telemetry_failure(
    monkeypatch: pytest.MonkeyPatch, entrypoint: str
) -> None:
    governed = govern_graph(build_graph())
    calls = _install_failing_transport(monkeypatch, entrypoint)
    with pytest.raises(RuntimeError) as exc:
        _drive(governed, entrypoint, {"mode": "error"})
    assert "govern-graph-fixture-error" in str(exc.value)
    assert "telemetry-transport-down" not in str(exc.value)
    assert calls, "telemetry transport was never exercised"


@pytest.mark.parametrize("entrypoint", ["ainvoke", "astream"])
def test_f6_cancellation_survives_telemetry_failure(
    monkeypatch: pytest.MonkeyPatch, entrypoint: str
) -> None:
    governed = govern_graph(build_graph())
    calls = _install_failing_transport(monkeypatch, entrypoint)

    async def run() -> None:
        if entrypoint == "ainvoke":
            awaitable = governed.ainvoke({"mode": "cancel"})
        else:
            awaitable = _drain_async(governed.astream({"mode": "cancel"}))
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(awaitable, timeout=0.2)

    asyncio.run(run())
    assert calls, "telemetry transport was never exercised during cancellation"
