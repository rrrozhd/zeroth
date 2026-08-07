"""Conformance tests for the govern_graph observed-mode LangGraph wrapper (ZER-2).

Gated behind the ``gateway-conformance`` group and the ``langgraph_conformance``
marker (deselected by default): run with ``-m langgraph_conformance``.
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime
from inspect import isawaitable
from typing import Any, TypedDict

import pytest

pytest.importorskip("langgraph", reason="requires the gateway-conformance dependency group")

from langchain_core.callbacks import BaseCallbackHandler, BaseCallbackManager
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from zeroth.contracts.langgraph_gateway.models import GovernanceLevel
from zeroth.econ.instrumentation.integrations.langgraph import InstrumentedLangGraph
from zeroth.econ.instrumentation.runtime import get_runtime
from zeroth.governance.langgraph_gateway.capabilities import (
    CapabilityReporter,
    NoCapabilityEvidenceProvider,
)
from zeroth.integrations.langgraph import (
    LangGraphGatewayError,
    RunStartContext,
    ZerothGovernanceCallbackHandler,
    govern_graph,
)
from zeroth.integrations.langgraph._callbacks import merge_governance_callbacks
from zeroth.integrations.langgraph._correlation import current_correlation

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


def _event_projection(events: list[dict[str, Any]]) -> list[tuple[Any, Any, Any]]:
    return [(event["event"], event["name"], event["data"]) for event in events]


def _v3_event_projection(events: list[dict[str, Any]]) -> list[tuple[Any, Any, Any, Any]]:
    return [
        (
            event["method"],
            event["params"]["namespace"],
            event["params"].get("data"),
            event["seq"],
        )
        for event in events
    ]


def _zeroth_config(correlation: str, **config: Any) -> dict[str, Any]:
    payload = base64.urlsafe_b64encode(
        json.dumps({"correlation_id": correlation}).encode()
    ).decode()
    return {
        **config,
        "configurable": {"_zeroth": f"header.{payload}.signature"},
    }


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

    def astream_events(self, *args: Any, **kwargs: Any) -> Any:
        self.configs.append(kwargs.get("config", args[1] if len(args) >= 2 else None))
        return self._inner.astream_events(*args, **kwargs)


class _GatewaySpy:
    def __init__(self, *, fail_token: str | None = None, order: list[Any] | None = None):
        self.fail_token = fail_token
        self.order = order if order is not None else []
        self.starts: list[tuple[str, str]] = []

    def start_run(self, token: str, correlation: str) -> None:
        self.starts.append((token, correlation))
        self.order.append(("gateway", correlation))
        if token == self.fail_token:
            raise LangGraphGatewayError()


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


def test_astream_events_is_equivalent_and_governed_once() -> None:
    graph = build_graph()
    payload = {"mode": "echo", "text": "events"}
    direct_callback = _CountingHandler()
    wrapped_callback = _CountingHandler()
    starts: list[RunStartContext] = []
    governed = govern_graph(graph, on_run_start=starts.append)

    direct = asyncio.run(
        _drain_async(graph.astream_events(dict(payload), config={"callbacks": [direct_callback]}))
    )
    wrapped = asyncio.run(
        _drain_async(
            governed.astream_events(dict(payload), config={"callbacks": [wrapped_callback]})
        )
    )

    assert _event_projection(wrapped) == _event_projection(direct)
    assert wrapped_callback.chain_starts == direct_callback.chain_starts
    assert wrapped_callback.chain_ends == direct_callback.chain_ends
    assert [event.entrypoint for event in starts] == ["astream_events"]
    assert governed._handler.completed_spans


def test_batch_preserves_configs_results_exceptions_and_governs_each_input() -> None:
    graph = build_graph()
    inputs = [
        {"mode": "echo", "text": "first"},
        {"mode": "echo", "text": "second"},
    ]
    direct_callback = _CountingHandler()
    wrapped_callback = _CountingHandler()
    starts: list[RunStartContext] = []
    governed = govern_graph(graph, on_run_start=starts.append)

    direct = graph.batch(inputs, config={"callbacks": [direct_callback]})
    wrapped = governed.batch(inputs, config={"callbacks": [wrapped_callback]})
    assert wrapped == direct
    assert wrapped_callback.chain_starts == direct_callback.chain_starts
    assert wrapped_callback.chain_ends == direct_callback.chain_ends
    assert [event.entrypoint for event in starts] == ["invoke", "invoke"]

    configs = [{"tags": ["first"]}, {"tags": ["second"]}]
    assert governed.batch(inputs, config=configs) == graph.batch(inputs, config=configs)

    mixed = governed.batch(
        [{"mode": "echo", "text": "ok"}, {"mode": "error"}],
        return_exceptions=True,
    )
    assert mixed[0]["result"] == "echo:ok"
    assert isinstance(mixed[1], RuntimeError)
    assert str(mixed[1]) == "govern-graph-fixture-error"


def test_abatch_is_equivalent_governed_and_cancellable() -> None:
    graph = build_graph()
    inputs = [
        {"mode": "echo", "text": "first"},
        {"mode": "echo", "text": "second"},
    ]
    starts: list[RunStartContext] = []
    governed = govern_graph(graph, on_run_start=starts.append)

    direct = asyncio.run(graph.abatch(inputs, config=[{"tags": ["a"]}, {"tags": ["b"]}]))
    wrapped = asyncio.run(governed.abatch(inputs, config=[{"tags": ["a"]}, {"tags": ["b"]}]))
    assert wrapped == direct
    assert [event.entrypoint for event in starts] == ["ainvoke", "ainvoke"]

    async def cancel(target: Any) -> None:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                target.abatch([{"mode": "cancel"}, {"mode": "echo", "text": "x"}]),
                timeout=0.2,
            )

    asyncio.run(cancel(graph))
    asyncio.run(cancel(governed))


def test_batch_and_abatch_deliver_bound_and_aligned_configs_once() -> None:
    inputs = [
        {"mode": "echo", "text": "first"},
        {"mode": "echo", "text": "second"},
    ]

    def assert_configs(spy: _ConfigSpy, governed: Any) -> None:
        assert len(spy.configs) == 2
        delivered = sorted(spy.configs, key=lambda config: config["metadata"]["item"])
        for item, config in enumerate(delivered):
            callbacks = list(config["callbacks"])
            assert callbacks.count(governed._handler) == 1
            assert config["tags"] == ["bound"]
            assert config["metadata"] == {"item": item}

    sync_spy = _ConfigSpy(build_graph())
    sync_governed = govern_graph(sync_spy).with_config({"tags": ["bound"]})
    sync_governed.batch(
        inputs,
        config=[{"metadata": {"item": 0}}, {"metadata": {"item": 1}}],
    )
    assert_configs(sync_spy, sync_governed)

    async_spy = _ConfigSpy(build_graph())
    async_governed = govern_graph(async_spy).with_config({"tags": ["bound"]})
    asyncio.run(
        async_governed.abatch(
            inputs,
            config=[{"metadata": {"item": 0}}, {"metadata": {"item": 1}}],
        )
    )
    assert_configs(async_spy, async_governed)


def test_batch_gateway_runs_before_hook_and_fails_closed_per_item() -> None:
    order: list[Any] = []
    good = _zeroth_config("good", max_concurrency=1)
    bad = _zeroth_config("bad", max_concurrency=1)
    gateway = _GatewaySpy(
        fail_token=bad["configurable"]["_zeroth"],
        order=order,
    )
    governed = govern_graph(
        build_graph(),
        gateway_client=gateway,
        on_run_start=lambda event: order.append(("hook", event.entrypoint)),
    )

    results = governed.batch(
        [
            {"mode": "echo", "text": "good"},
            {"mode": "echo", "text": "blocked"},
        ],
        config=[good, bad],
        return_exceptions=True,
    )

    assert results[0]["result"] == "echo:good"
    assert isinstance(results[1], LangGraphGatewayError)
    assert gateway.starts == [
        (good["configurable"]["_zeroth"], "good"),
        (bad["configurable"]["_zeroth"], "bad"),
    ]
    assert order == [
        ("gateway", "good"),
        ("hook", "invoke"),
        ("gateway", "bad"),
    ]


def test_abatch_shared_gateway_config_attests_each_input() -> None:
    config = _zeroth_config("shared", max_concurrency=1)
    gateway = _GatewaySpy()
    starts: list[RunStartContext] = []
    governed = govern_graph(build_graph(), gateway_client=gateway, on_run_start=starts.append)

    results = asyncio.run(
        governed.abatch(
            [
                {"mode": "echo", "text": "first"},
                {"mode": "echo", "text": "second"},
            ],
            config=config,
        )
    )

    assert [result["result"] for result in results] == ["echo:first", "echo:second"]
    assert gateway.starts == [
        (config["configurable"]["_zeroth"], "shared"),
        (config["configurable"]["_zeroth"], "shared"),
    ]
    assert [event.entrypoint for event in starts] == ["ainvoke", "ainvoke"]


def test_astream_events_preserves_errors_and_cancellation() -> None:
    graph = build_graph()
    governed = govern_graph(graph)

    with pytest.raises(RuntimeError) as direct_error:
        asyncio.run(_drain_async(graph.astream_events({"mode": "error"})))
    with pytest.raises(RuntimeError) as governed_error:
        asyncio.run(_drain_async(governed.astream_events({"mode": "error"})))
    assert type(governed_error.value) is type(direct_error.value)
    assert str(governed_error.value).splitlines()[0] == str(direct_error.value).splitlines()[0]

    for target in (graph, governed):

        async def cancel(current: Any) -> None:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    _drain_async(current.astream_events({"mode": "cancel"})),
                    timeout=0.2,
                )

        asyncio.run(cancel(target))


def test_astream_events_reuses_econ_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = get_runtime()
    runtime.config.enabled = True
    captured: list[Any] = []

    async def capture(event: Any) -> None:
        captured.append(event)

    monkeypatch.setattr(runtime.transport, "aenqueue_execution", capture)
    asyncio.run(
        _drain_async(govern_graph(build_graph()).astream_events({"mode": "echo", "text": "cost"}))
    )

    assert len(captured) == 1
    assert captured[0].metadata["operation"] == "astream_events"


def test_astream_events_applies_bound_config_and_fails_gateway_closed() -> None:
    spy = _ConfigSpy(build_graph())
    governed = govern_graph(spy).with_config({"tags": ["bound"]})
    asyncio.run(_drain_async(governed.astream_events({"mode": "echo", "text": "bound"})))
    delivered = spy.configs[-1]
    assert delivered["tags"] == ["bound"]
    assert list(delivered["callbacks"]).count(governed._handler) == 1

    config = _zeroth_config("blocked")
    order: list[Any] = []
    gateway = _GatewaySpy(
        fail_token=config["configurable"]["_zeroth"],
        order=order,
    )
    blocked = govern_graph(
        build_graph(),
        gateway_client=gateway,
        on_run_start=lambda event: order.append(("hook", event.entrypoint)),
    )
    with pytest.raises(LangGraphGatewayError):
        blocked.astream_events({"mode": "echo", "text": "blocked"}, config=config)
    assert order == [("gateway", "blocked")]


def test_astream_events_v3_preserves_awaited_driver_governance_and_econ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        graph = build_graph()
        payload = {"mode": "echo", "text": "v3"}
        direct_callback = _CountingHandler()
        wrapped_callback = _CountingHandler()
        starts: list[RunStartContext] = []
        governed = govern_graph(graph, on_run_start=starts.append)
        runtime = get_runtime()
        runtime.config.enabled = True
        captured: list[Any] = []

        async def capture(event: Any) -> None:
            captured.append(event)

        monkeypatch.setattr(runtime.transport, "aenqueue_execution", capture)
        direct_run = await graph.astream_events(
            dict(payload), config={"callbacks": [direct_callback]}, version="v3"
        )
        governed_awaitable = governed.astream_events(
            dict(payload), config={"callbacks": [wrapped_callback]}, version="v3"
        )
        assert isawaitable(governed_awaitable)
        wrapped_run = await governed_awaitable
        assert type(wrapped_run) is type(direct_run)

        async with direct_run:
            direct = [event async for event in direct_run]
        async with wrapped_run:
            wrapped = [event async for event in wrapped_run]

        assert _v3_event_projection(wrapped) == _v3_event_projection(direct)
        assert wrapped_callback.chain_starts == direct_callback.chain_starts
        assert wrapped_callback.chain_ends == direct_callback.chain_ends
        assert [event.entrypoint for event in starts] == ["astream_events"]
        assert governed._handler.completed_spans
        assert len(captured) == 1
        assert captured[0].metadata["operation"] == "astream_events"

    asyncio.run(scenario())


def test_astream_events_v3_applies_bound_gateway_and_private_context() -> None:
    async def scenario() -> None:
        seen: list[str | None] = []

        def observe(state: _State) -> _State:
            seen.append(current_correlation())
            return {"result": f"echo:{state.get('text', '')}"}

        builder = StateGraph(_State)
        builder.add_node("observe", observe)
        builder.add_edge(START, "observe")
        builder.add_edge("observe", END)
        spy = _ConfigSpy(builder.compile())
        config = _zeroth_config("v3-private", tags=["bound"])
        gateway = _GatewaySpy()
        governed = govern_graph(spy, gateway_client=gateway).with_config(config)

        run = await governed.astream_events({"text": "bound"}, version="v3")
        values = []
        async with run:
            async for value in run.values:
                assert current_correlation() is None
                values.append(value)

        assert values[-1]["result"] == "echo:bound"
        assert seen == ["v3-private"]
        assert current_correlation() is None
        assert gateway.starts == [(config["configurable"]["_zeroth"], "v3-private")]
        delivered = spy.configs[-1]
        assert delivered["tags"] == ["bound"]
        assert list(delivered["callbacks"]).count(governed._handler) == 1

        blocked_gateway = _GatewaySpy(fail_token=config["configurable"]["_zeroth"])
        blocked = govern_graph(build_graph(), gateway_client=blocked_gateway).with_config(config)
        with pytest.raises(LangGraphGatewayError):
            blocked.astream_events({"mode": "echo"}, version="v3")

    asyncio.run(scenario())


def test_astream_events_v3_preserves_cancellation_and_abort() -> None:
    async def cancel(target: Any) -> None:
        run = await target.astream_events({"mode": "cancel"}, version="v3")
        async with run:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(run.output(), timeout=0.2)

    graph = build_graph()
    asyncio.run(cancel(graph))
    asyncio.run(cancel(govern_graph(graph)))


class _FrozenDateTime:
    @classmethod
    def now(cls, tz: Any = None) -> datetime:
        return datetime(2026, 1, 1, tzinfo=tz)


def test_batch_emits_distinct_econ_execution_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = get_runtime()
    runtime.config.enabled = True
    captured: list[Any] = []
    monkeypatch.setattr(
        "zeroth.econ.instrumentation.integrations.langgraph.datetime",
        _FrozenDateTime,
    )
    monkeypatch.setattr(
        runtime.transport, "enqueue_execution", lambda event: captured.append(event)
    )

    govern_graph(build_graph()).batch(
        [
            {"mode": "echo", "text": "first"},
            {"mode": "echo", "text": "second"},
        ],
        config={"max_concurrency": 2},
    )

    assert len(captured) == 2
    assert len({event.execution_id for event in captured}) == 2


def test_abatch_emits_distinct_econ_execution_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = get_runtime()
    runtime.config.enabled = True
    captured: list[Any] = []

    async def capture(event: Any) -> None:
        captured.append(event)

    monkeypatch.setattr(
        "zeroth.econ.instrumentation.integrations.langgraph.datetime",
        _FrozenDateTime,
    )
    monkeypatch.setattr(runtime.transport, "aenqueue_execution", capture)

    asyncio.run(
        govern_graph(build_graph()).abatch(
            [
                {"mode": "echo", "text": "first"},
                {"mode": "echo", "text": "second"},
            ],
            config={"max_concurrency": 2},
        )
    )

    assert len(captured) == 2
    assert len({event.execution_id for event in captured}) == 2
