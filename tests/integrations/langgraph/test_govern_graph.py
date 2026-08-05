"""Conformance tests for the govern_graph observed-mode LangGraph wrapper (ZER-2).

Gated behind the ``gateway-conformance`` group and the ``langgraph_conformance``
marker (deselected by default): run with ``-m langgraph_conformance``.
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

from zeroth.core.langgraph_gateway.capabilities import (
    CapabilityReporter,
    NoCapabilityEvidenceProvider,
)
from zeroth.core.langgraph_gateway.models import GovernanceLevel
from zeroth.econ.instrumentation.integrations.langgraph import InstrumentedLangGraph
from zeroth.econ.instrumentation.runtime import get_runtime
from zeroth.integrations.langgraph import (
    RunStartContext,
    ZerothGovernanceCallbackHandler,
    govern_graph,
)
from zeroth.integrations.langgraph._callbacks import merge_governance_callbacks

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
    writer({"seq": 3, "value": "end"})
    return {"result": f"emit:{state.get('text', '')}"}


def _boom(_state: _State) -> _State:
    raise RuntimeError("govern-graph-fixture-error")


async def _slow(_state: _State) -> _State:
    await asyncio.sleep(30)
    return {"result": "slow-done"}


def _route(state: _State) -> str:
    return state.get("mode", "echo")


def build_graph() -> Any:
    """Compile a small, deterministic StateGraph mirroring the conformance fixture."""
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
    """User callback handler that tallies how often it is invoked per event."""

    def __init__(self) -> None:
        self.chain_starts = 0
        self.chain_ends = 0

    def on_chain_start(self, *args: Any, **kwargs: Any) -> None:
        self.chain_starts += 1

    def on_chain_end(self, *args: Any, **kwargs: Any) -> None:
        self.chain_ends += 1


@pytest.fixture(autouse=True)
def _transparent_econ():
    """Disable econ capture so the reused delegate is a pure pass-through.

    Equivalence checks compare the wrapper against the bare graph without
    telemetry side effects; the reuse test re-enables econ explicitly.
    """
    runtime = get_runtime()
    original = runtime.config.enabled
    runtime.config.enabled = False
    try:
        yield
    finally:
        runtime.config.enabled = original


async def _drain_async(astream: Any) -> list[Any]:
    return [chunk async for chunk in astream]


class _ConfigSpy:
    """Record the RunnableConfig each invoke receives, then delegate to the graph."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.configs: list[Any] = []

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        self.configs.append(kwargs.get("config", args[1] if len(args) >= 2 else None))
        return self._inner.invoke(*args, **kwargs)

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        self.configs.append(kwargs.get("config", args[1] if len(args) >= 2 else None))
        return await self._inner.ainvoke(*args, **kwargs)

    def stream(self, *args: Any, **kwargs: Any) -> Any:
        self.configs.append(kwargs.get("config", args[1] if len(args) >= 2 else None))
        return self._inner.stream(*args, **kwargs)

    def astream(self, *args: Any, **kwargs: Any) -> Any:
        self.configs.append(kwargs.get("config", args[1] if len(args) >= 2 else None))
        return self._inner.astream(*args, **kwargs)


def test_r2_invoke_result_is_byte_for_byte_equivalent() -> None:
    graph = build_graph()
    payload = {"mode": "echo", "text": "hello"}
    direct = graph.invoke(dict(payload))
    wrapped = govern_graph(graph).invoke(dict(payload))
    assert json.dumps(wrapped, sort_keys=True) == json.dumps(direct, sort_keys=True)


def test_r2_ainvoke_result_is_byte_for_byte_equivalent() -> None:
    graph = build_graph()
    payload = {"mode": "echo", "text": "hello"}
    direct = asyncio.run(graph.ainvoke(dict(payload)))
    wrapped = asyncio.run(govern_graph(graph).ainvoke(dict(payload)))
    assert json.dumps(wrapped, sort_keys=True) == json.dumps(direct, sort_keys=True)


def test_r3_stream_chunks_are_identical_and_ordered() -> None:
    graph = build_graph()
    payload = {"mode": "emit", "text": "streamed"}
    direct = list(graph.stream(dict(payload), stream_mode="custom"))
    wrapped = list(govern_graph(graph).stream(dict(payload), stream_mode="custom"))
    assert wrapped == direct
    assert [chunk["seq"] for chunk in wrapped] == [1, 2, 3]


def test_r3_astream_chunks_are_identical_and_ordered() -> None:
    graph = build_graph()
    payload = {"mode": "emit", "text": "streamed"}
    direct = asyncio.run(_drain_async(graph.astream(dict(payload), stream_mode="custom")))
    wrapped = asyncio.run(
        _drain_async(govern_graph(graph).astream(dict(payload), stream_mode="custom"))
    )
    assert wrapped == direct
    assert [chunk["seq"] for chunk in wrapped] == [1, 2, 3]


def test_r4_user_callback_fires_once_per_event_and_zeroth_is_present() -> None:
    graph = build_graph()
    payload = {"mode": "echo", "text": "cb"}

    direct_cb = _CountingHandler()
    graph.invoke(dict(payload), config={"callbacks": [direct_cb]})

    wrapped_cb = _CountingHandler()
    governed = govern_graph(graph)
    governed.invoke(dict(payload), config={"callbacks": [wrapped_cb]})

    # Sanity: events actually happened, and wrapping neither added, dropped nor
    # duplicated the user handler's invocations.
    assert direct_cb.chain_starts >= 1
    assert wrapped_cb.chain_starts == direct_cb.chain_starts
    assert wrapped_cb.chain_ends == direct_cb.chain_ends


def test_r4_wrapper_delivers_both_handlers_into_the_graph_config() -> None:
    # Observe the config the wrapped graph actually receives (not a re-derivation):
    # inject_governance_handler must deliver BOTH the user and Zeroth handlers.
    spy = _ConfigSpy(build_graph())
    governed = govern_graph(spy)
    user = _CountingHandler()
    governed.invoke({"mode": "echo", "text": "x"}, config={"callbacks": [user]})
    delivered = spy.configs[-1]["callbacks"]
    assert user in delivered
    assert governed._handler in delivered
    assert delivered.count(governed._handler) == 1


@pytest.mark.parametrize("marker", [True, "caller-value"], ids=("boolean", "string"))
@pytest.mark.parametrize("entrypoint", ["invoke", "ainvoke", "stream", "astream"])
def test_public_config_cannot_request_latest_checkpoint(marker: object, entrypoint: str) -> None:
    spy = _ConfigSpy(build_graph())
    governed = govern_graph(spy)
    config = {
        "configurable": {
            "thread_id": "thread-1",
            "checkpoint_id": "caller-checkpoint",
            "checkpoint_ns": "caller|child",
            "checkpoint_map": {"caller|child": "caller-checkpoint"},
            "_zeroth_resume_latest_checkpoint": marker,
        }
    }
    payload = {"mode": "echo", "text": "checkpoint"}

    if entrypoint == "invoke":
        governed.invoke(payload, config)
    elif entrypoint == "ainvoke":
        asyncio.run(governed.ainvoke(payload, config))
    elif entrypoint == "stream":
        list(governed.stream(payload, config))
    else:
        asyncio.run(_drain_async(governed.astream(payload, config)))

    delivered = spy.configs[-1]["configurable"]
    assert delivered["checkpoint_id"] == "caller-checkpoint"
    assert delivered["checkpoint_ns"] == "caller|child"
    assert delivered["checkpoint_map"] == {"caller|child": "caller-checkpoint"}
    assert delivered["_zeroth_resume_latest_checkpoint"] is marker


def test_r5_error_propagates_identically_on_all_entrypoints() -> None:
    graph = build_graph()
    governed = govern_graph(graph)
    payload = {"mode": "error"}

    def run_invoke(target: Any) -> Any:
        return target.invoke(dict(payload))

    def run_stream(target: Any) -> Any:
        return list(target.stream(dict(payload)))

    def run_ainvoke(target: Any) -> Any:
        return asyncio.run(target.ainvoke(dict(payload)))

    def run_astream(target: Any) -> Any:
        return asyncio.run(_drain_async(target.astream(dict(payload))))

    for runner in (run_invoke, run_stream, run_ainvoke, run_astream):
        with pytest.raises(RuntimeError) as direct_exc:
            runner(graph)
        with pytest.raises(RuntimeError) as wrapped_exc:
            runner(governed)
        assert type(wrapped_exc.value) is type(direct_exc.value)
        assert str(wrapped_exc.value) == str(direct_exc.value)
        assert "govern-graph-fixture-error" in str(wrapped_exc.value)


def test_r6_attributes_delegate_to_the_wrapped_graph() -> None:
    graph = build_graph()
    governed = govern_graph(graph)

    assert governed.get_graph().nodes.keys() == graph.get_graph().nodes.keys()
    assert governed.name == graph.name

    with pytest.raises(AttributeError):
        _ = governed.this_attribute_does_not_exist_zzz


def test_r7_merge_handles_absent_list_and_manager_without_clobbering() -> None:
    handler = ZerothGovernanceCallbackHandler()

    # Absent config / absent callbacks -> just the handler.
    assert merge_governance_callbacks(None, handler)["callbacks"] == [handler]
    assert merge_governance_callbacks({}, handler)["callbacks"] == [handler]

    # A list of user handlers -> appended; user preserved; idempotent (no dup).
    user = _CountingHandler()
    merged = merge_governance_callbacks({"callbacks": [user]}, handler)
    assert merged["callbacks"] == [user, handler]
    again = merge_governance_callbacks(merged, handler)
    assert again["callbacks"].count(handler) == 1
    assert user in again["callbacks"]

    # A BaseCallbackManager -> handler added to a copy; caller's manager untouched.
    manager = BaseCallbackManager(handlers=[user])
    out = merge_governance_callbacks({"callbacks": manager}, handler)["callbacks"]
    assert isinstance(out, BaseCallbackManager)
    assert user in out.handlers
    assert handler in out.handlers
    assert handler not in manager.handlers

    # Other config keys survive the merge.
    preserved = merge_governance_callbacks({"tags": ["t"], "callbacks": [user]}, handler)
    assert preserved["tags"] == ["t"]


def test_fa5_capability_floor_and_no_promotion_path() -> None:
    import zeroth.integrations.langgraph as lg_pkg

    # (1) Without downstream evidence, a run cannot be reported above admission.
    reporter = CapabilityReporter(NoCapabilityEvidenceProvider())
    assert asyncio.run(reporter.level_for_run("run-under-test")) is GovernanceLevel.ADMISSION

    # (2) The wrapper package registers no reporter / evidence provider and
    #     exposes no public symbol that could silently promote a run.
    for name in lg_pkg.__all__:
        obj = getattr(lg_pkg, name)
        assert not isinstance(obj, CapabilityReporter)
        assert obj is not GovernanceLevel.OBSERVED
        assert obj is not GovernanceLevel.ENFORCED
        # Not a live evidence-provider instance (a provider exposes this coroutine).
        assert not (not isinstance(obj, type) and hasattr(obj, "evidence_for_run"))

    # (3) The stability seam carries no governance / attestation field.
    seam_fields = set(RunStartContext.__dataclass_fields__)
    assert not (
        seam_fields & {"governance_level", "governance", "attestation", "level", "evidence"}
    )
    ctx = RunStartContext(graph=object(), handler_registered=True, entrypoint="invoke")
    assert not hasattr(ctx, "governance_level")

    # (4) A constructed wrapper holds no promoted level; its handler is a plain
    #     callback handler, not an evidence provider.
    governed = govern_graph(object())
    assert isinstance(governed._handler, ZerothGovernanceCallbackHandler)
    assert not hasattr(governed._handler, "evidence_for_run")
    assert not hasattr(governed, "governance_level")


def test_r9_ainvoke_cancellation_is_equivalent() -> None:
    graph = build_graph()
    governed = govern_graph(graph)
    payload = {"mode": "cancel"}

    async def cancel_ainvoke(target: Any) -> None:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(target.ainvoke(dict(payload)), timeout=0.2)

    asyncio.run(cancel_ainvoke(graph))
    asyncio.run(cancel_ainvoke(governed))


def test_r9_astream_cancellation_is_equivalent() -> None:
    graph = build_graph()
    governed = govern_graph(graph)
    payload = {"mode": "cancel"}

    async def cancel_astream(target: Any) -> None:
        async def drain() -> None:
            async for _ in target.astream(dict(payload)):
                pass

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(drain(), timeout=0.2)

    asyncio.run(cancel_astream(graph))
    asyncio.run(cancel_astream(governed))


def test_r11_default_on_run_start_is_noop_byte_identical() -> None:
    graph = build_graph()
    payload = {"mode": "echo", "text": "seam"}
    direct = graph.invoke(dict(payload))
    omitted = govern_graph(graph).invoke(dict(payload))
    explicit_none = govern_graph(graph, on_run_start=None).invoke(dict(payload))
    assert json.dumps(omitted, sort_keys=True) == json.dumps(direct, sort_keys=True)
    assert json.dumps(explicit_none, sort_keys=True) == json.dumps(direct, sort_keys=True)


def test_r11_on_run_start_fires_exactly_once_per_entrypoint() -> None:
    graph = build_graph()
    events: list[RunStartContext] = []
    governed = govern_graph(graph, on_run_start=events.append)

    governed.invoke({"mode": "echo", "text": "x"})
    assert len(events) == 1

    asyncio.run(governed.ainvoke({"mode": "echo", "text": "x"}))
    assert len(events) == 2

    list(governed.stream({"mode": "emit", "text": "x"}, stream_mode="custom"))
    assert len(events) == 3

    asyncio.run(_drain_async(governed.astream({"mode": "emit", "text": "x"}, stream_mode="custom")))
    assert len(events) == 4

    first = events[0]
    assert first.graph is graph
    assert first.handler_registered is True
    assert first.entrypoint == "invoke"
    assert {event.entrypoint for event in events} == {"invoke", "ainvoke", "stream", "astream"}


def test_r1_reuses_econ_delegation_for_cost_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    graph = build_graph()
    governed = govern_graph(graph)

    # Structural reuse: entrypoints route through InstrumentedLangGraph.
    assert isinstance(governed._delegate, InstrumentedLangGraph)

    # Behavioural reuse: with econ enabled, invoking through the wrapper drives
    # the econ delegate's capture path (an execution event is enqueued).
    runtime = get_runtime()
    runtime.config.enabled = True
    captured: list[Any] = []
    monkeypatch.setattr(
        runtime.transport, "enqueue_execution", lambda event: captured.append(event)
    )
    governed.invoke({"mode": "echo", "text": "cost"})
    assert captured, "expected the reused econ delegation to capture an execution event"
