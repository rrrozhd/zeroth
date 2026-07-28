"""Audit-hardening gate tests for the govern_graph wrapper (ZER-2, audit-1).

These cover three findings that the original conformance suite missed:

* **F1** -- ``with_config(...)`` must stay governed (not delegate to a bare,
  ungoverned ``RunnableBinding``).
* **F2** -- callback-merge dedup must key on handler *type/identity*, never on
  arbitrary user-callback equality, so governance is neither duplicated nor
  silently suppressed.
* **F3** -- with econ instrumentation ENABLED, a telemetry emit failure must
  never change the graph's result, mask its exception, or alter cancellation.

Self-contained (own fixtures/helpers) so it stays under the file-size budget and
does not couple to the sibling conformance module. Gated behind the same
``langgraph_conformance`` marker; run with ``-o addopts= -m langgraph_conformance``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, TypedDict

import pytest

pytest.importorskip("langgraph", reason="requires the gateway-conformance dependency group")

from langchain_core.callbacks import BaseCallbackHandler, BaseCallbackManager
from langgraph.config import get_stream_writer
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


def _emit(state: _State) -> _State:
    writer = get_stream_writer()
    writer({"seq": 1, "value": "start"})
    writer({"seq": 2, "value": state.get("text", "")})
    return {"result": f"emit:{state.get('text', '')}"}


def _boom(_state: _State) -> _State:
    raise RuntimeError("govern-graph-fixture-error")


async def _slow(_state: _State) -> _State:
    await asyncio.sleep(30)
    return {"result": "slow-done"}


def _route(state: _State) -> str:
    return state.get("mode", "echo")


def build_graph() -> Any:
    """Compile a small, deterministic StateGraph with echo/emit/error/cancel nodes."""
    builder = StateGraph(_State)
    builder.add_node("echo", _echo)
    builder.add_node("emit", _emit)
    builder.add_node("error", _boom)
    builder.add_node("cancel", _slow)
    builder.add_conditional_edges(
        START,
        _route,
        {"echo": "echo", "emit": "emit", "error": "error", "cancel": "cancel"},
    )
    for node in ("echo", "emit", "error", "cancel"):
        builder.add_edge(node, END)
    return builder.compile()


class _CountingHandler(BaseCallbackHandler):
    """Plain user handler; identity/equality default (no ``__eq__`` override)."""


class _HostileEqHandler(BaseCallbackHandler):
    """User handler whose ``__eq__`` matches everything -- must not fool dedup."""

    def __eq__(self, _other: Any) -> bool:
        return True

    def __hash__(self) -> int:
        return id(self)


class _ConfigSpy:
    """Record the RunnableConfig each invoke receives, then delegate to the graph."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.configs: list[Any] = []

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        self.configs.append(kwargs.get("config", args[1] if len(args) >= 2 else None))
        return self._inner.invoke(*args, **kwargs)


@pytest.fixture(autouse=True)
def _transparent_econ():
    """Disable econ capture by default; F3 tests re-enable it explicitly."""
    runtime = get_runtime()
    original = runtime.config.enabled
    runtime.config.enabled = False
    try:
        yield
    finally:
        runtime.config.enabled = original


async def _drain_async(astream: Any) -> list[Any]:
    return [chunk async for chunk in astream]


def _handlers(callbacks: Any) -> list[Any]:
    """Return the handler sequence from a list- or manager-shaped callbacks value."""
    return list(callbacks.handlers) if hasattr(callbacks, "handlers") else list(callbacks)


def _governance_count(callbacks: Any) -> int:
    """Count governance handlers by TYPE (never equality)."""
    return sum(isinstance(h, ZerothGovernanceCallbackHandler) for h in _handlers(callbacks))


# --------------------------------------------------------------------------- F2


def test_f2_nested_govern_graph_installs_single_handler_list() -> None:
    spy = _ConfigSpy(build_graph())
    governed = govern_graph(govern_graph(spy))
    governed.invoke({"mode": "echo", "text": "x"}, config={"callbacks": [_CountingHandler()]})
    assert _governance_count(spy.configs[-1]["callbacks"]) == 1


def test_f2_nested_govern_graph_installs_single_handler_manager() -> None:
    spy = _ConfigSpy(build_graph())
    governed = govern_graph(govern_graph(spy))
    manager = BaseCallbackManager(handlers=[_CountingHandler()])
    governed.invoke({"mode": "echo", "text": "x"}, config={"callbacks": manager})
    delivered = spy.configs[-1]["callbacks"]
    assert isinstance(delivered, BaseCallbackManager)
    assert _governance_count(delivered) == 1


def test_f2_preinstalled_governance_handler_not_duplicated_list() -> None:
    spy = _ConfigSpy(build_graph())
    preinstalled = ZerothGovernanceCallbackHandler()
    govern_graph(spy).invoke({"mode": "echo", "text": "x"}, config={"callbacks": [preinstalled]})
    delivered = spy.configs[-1]["callbacks"]
    assert _governance_count(delivered) == 1
    assert any(h is preinstalled for h in _handlers(delivered))


def test_f2_preinstalled_governance_handler_not_duplicated_manager() -> None:
    spy = _ConfigSpy(build_graph())
    preinstalled = ZerothGovernanceCallbackHandler()
    manager = BaseCallbackManager(handlers=[preinstalled])
    govern_graph(spy).invoke({"mode": "echo", "text": "x"}, config={"callbacks": manager})
    delivered = spy.configs[-1]["callbacks"]
    assert _governance_count(delivered) == 1
    assert any(h is preinstalled for h in _handlers(delivered))


def test_f2_hostile_user_eq_does_not_suppress_governance_list() -> None:
    spy = _ConfigSpy(build_graph())
    governed = govern_graph(spy)
    governed.invoke({"mode": "echo", "text": "x"}, config={"callbacks": [_HostileEqHandler()]})
    delivered = spy.configs[-1]["callbacks"]
    # Identity, never `in`/`==`: the hostile handler matches everything by equality.
    assert any(h is governed._handler for h in _handlers(delivered))
    assert _governance_count(delivered) == 1


def test_f2_hostile_user_eq_does_not_suppress_governance_manager() -> None:
    spy = _ConfigSpy(build_graph())
    governed = govern_graph(spy)
    manager = BaseCallbackManager(handlers=[_HostileEqHandler()])
    governed.invoke({"mode": "echo", "text": "x"}, config={"callbacks": manager})
    delivered = spy.configs[-1]["callbacks"]
    assert any(h is governed._handler for h in _handlers(delivered))
    assert _governance_count(delivered) == 1


# --------------------------------------------------------------------------- F1


def test_f1_with_config_fires_on_run_start_exactly_once() -> None:
    graph = build_graph()
    events: list[Any] = []
    reconfigured = govern_graph(graph, on_run_start=events.append).with_config(
        {"tags": ["t"], "metadata": {"k": "v"}}
    )
    reconfigured.invoke({"mode": "echo", "text": "x"})
    assert len(events) == 1
    assert events[0].entrypoint == "invoke"


def test_f1_with_config_delivers_single_governance_handler_and_binds_config() -> None:
    spy = _ConfigSpy(build_graph())
    reconfigured = govern_graph(spy).with_config({"tags": ["bound"]})
    reconfigured.invoke({"mode": "echo", "text": "x"})
    delivered = spy.configs[-1]
    assert _governance_count(delivered["callbacks"]) == 1
    assert "bound" in (delivered.get("tags") or [])


def test_f1_with_config_result_equivalent_and_delegation_intact() -> None:
    graph = build_graph()
    cfg = {"tags": ["t"]}
    direct = graph.with_config(cfg).invoke({"mode": "echo", "text": "hi"})
    reconfigured = govern_graph(graph).with_config(cfg)
    wrapped = reconfigured.invoke({"mode": "echo", "text": "hi"})
    assert json.dumps(wrapped, sort_keys=True) == json.dumps(direct, sort_keys=True)
    # Attribute delegation still reaches the underlying graph on the reconfigured wrapper.
    assert reconfigured.get_graph().nodes.keys() == graph.get_graph().nodes.keys()
    assert reconfigured.name == graph.name


def test_f1_with_config_streaming_variant_fires_once_and_is_equivalent() -> None:
    graph = build_graph()
    events: list[Any] = []
    reconfigured = govern_graph(graph, on_run_start=events.append).with_config({"tags": ["t"]})
    direct = list(
        graph.with_config({"tags": ["t"]}).stream(
            {"mode": "emit", "text": "s"}, stream_mode="custom"
        )
    )
    chunks = list(reconfigured.stream({"mode": "emit", "text": "s"}, stream_mode="custom"))
    assert chunks == direct
    assert len(events) == 1
    assert events[0].entrypoint == "stream"


def test_f1_pipe_composition_raises_clear_error() -> None:
    governed = govern_graph(build_graph())
    with pytest.raises(TypeError, match="compose the graph before governing"):
        _ = governed | (lambda payload: payload)


# --------------------------------------------------------------------------- F3


def _raise_enqueue(_event: Any) -> None:
    raise RuntimeError("telemetry-transport-down")


async def _araise_enqueue(_event: Any) -> None:
    raise RuntimeError("telemetry-transport-down")


def test_f3_result_survives_telemetry_failure_with_econ_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    governed = govern_graph(build_graph())
    runtime = get_runtime()
    runtime.config.enabled = True
    monkeypatch.setattr(runtime.transport, "enqueue_execution", _raise_enqueue)
    result = governed.invoke({"mode": "echo", "text": "resilient"})
    assert result["result"] == "echo:resilient"


def test_f3_upstream_error_not_masked_by_telemetry_failure_with_econ_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    governed = govern_graph(build_graph())
    runtime = get_runtime()
    runtime.config.enabled = True
    monkeypatch.setattr(runtime.transport, "enqueue_execution", _raise_enqueue)
    with pytest.raises(RuntimeError) as exc:
        governed.invoke({"mode": "error"})
    assert "govern-graph-fixture-error" in str(exc.value)
    # The telemetry failure must NOT surface in place of the application error.
    assert "telemetry-transport-down" not in str(exc.value)


def test_f3_cancellation_survives_telemetry_failure_with_econ_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    governed = govern_graph(build_graph())
    runtime = get_runtime()
    runtime.config.enabled = True
    monkeypatch.setattr(runtime.transport, "aenqueue_execution", _araise_enqueue)

    async def run() -> None:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(governed.ainvoke({"mode": "cancel"}), timeout=0.2)

    asyncio.run(run())
