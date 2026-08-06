"""Causal callback-ancestry conformance tests for the governance handler (ZER-3).

Gated behind the ``gateway-conformance`` group and the ``langgraph_conformance``
marker (deselected by default): run with
``uv run pytest -o addopts= -q tests/integrations/langgraph/``.

The concurrency / dedup class (FA2, FA4, R8) is front-loaded: a single handler
instance is shared across all concurrent runs, so these guard the lock-protected,
run-id-keyed state that must not corrupt across trees. The root / orphan class
(FA5) lives in the companion ``test_callback_ancestry_orphans.py``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import threading
import uuid
from typing import Any, TypedDict

import pytest

pytest.importorskip("langgraph", reason="requires the gateway-conformance dependency group")

from langchain_core.runnables import RunnableLambda
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from zeroth.core.signing import EnvHmacSigner
from zeroth.econ.instrumentation.runtime import get_runtime
from zeroth.integrations.langgraph import CausalSpan, ZerothGovernanceCallbackHandler, govern_graph
from zeroth.integrations.langgraph._correlation import _correlation_from_config
from zeroth.service.langgraph_gateway.context import ReservedContextClaims, ReservedContextCodec

pytestmark = pytest.mark.langgraph_conformance


# --------------------------------------------------------------------------- #
# Fixtures: fan-out + fan-in graphs (distinct parallel keys avoid a LastValue  #
# InvalidUpdateError) and a nested sub-runnable inside a node.                 #
# --------------------------------------------------------------------------- #


class _State(TypedDict, total=False):
    text: str
    a_out: str
    b_out: str
    result: str


def _a_plain(state: _State) -> _State:
    return {"a_out": "a"}


def _a_nested(state: _State) -> _State:
    # A nested sub-runnable invoked inside the node: LangChain propagates the
    # run tree via a contextvar, so this emits a child chain whose parent is
    # node "a" (not the root).
    RunnableLambda(lambda _x: {"inner": 1}, name="inner_a").invoke({"x": 1})
    return {"a_out": "a"}


def _b(state: _State) -> _State:
    return {"b_out": "b"}


def _c(state: _State) -> _State:
    return {"result": f"{state.get('a_out', '')}{state.get('b_out', '')}c"}


def _boom(_state: _State) -> _State:
    raise RuntimeError("ancestry-fixture-error")


def _emit(state: _State) -> _State:
    writer = get_stream_writer()
    writer({"seq": 1})
    writer({"seq": 2})
    return {"result": "emit"}


async def _slow(_state: _State) -> _State:
    await asyncio.sleep(30)
    return {"result": "slow"}


def _build_fanout(*, nested: bool) -> Any:
    builder = StateGraph(_State)
    builder.add_node("a", _a_nested if nested else _a_plain)
    builder.add_node("b", _b)
    builder.add_node("c", _c)
    builder.add_edge(START, "a")
    builder.add_edge(START, "b")
    builder.add_edge("a", "c")
    builder.add_edge("b", "c")
    builder.add_edge("c", END)
    return builder.compile()


def _build_error_graph() -> Any:
    builder = StateGraph(_State)
    builder.add_node("boom", _boom)
    builder.add_edge(START, "boom")
    builder.add_edge("boom", END)
    return builder.compile()


def _build_router() -> Any:
    builder = StateGraph(_State)
    builder.add_node("echo", lambda s: {"result": f"echo:{s.get('text', '')}"})
    builder.add_node("emit", _emit)
    builder.add_node("cancel", _slow)
    builder.add_conditional_edges(
        START, lambda s: s.get("text", "echo"), {"echo": "echo", "emit": "emit", "cancel": "cancel"}
    )
    for node in ("echo", "emit", "cancel"):
        builder.add_edge(node, END)
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


def _children(spans: list[CausalSpan], parent_id: str | None) -> list[CausalSpan]:
    return [s for s in spans if s.parent_run_id == parent_id]


async def _drain(astream: Any) -> list[Any]:
    return [chunk async for chunk in astream]


# --------------------------------------------------------------------------- #
# Concurrency / dedup class (front-loaded). Root / orphan: companion file.     #
# --------------------------------------------------------------------------- #


def test_r4_parallel_nodes_share_root_with_distinct_run_ids() -> None:
    """FA2: parallel a, b both parent=root, distinct full run ids, neither parents the other."""
    governed = govern_graph(_build_fanout(nested=False))
    governed.invoke({"text": "hi"})
    spans = governed._handler.completed_spans

    roots = [s for s in spans if s.parent_run_id is None]
    assert len(roots) == 1
    root_id = roots[0].run_id
    node_a = next(s for s in spans if s.name == "a")
    node_b = next(s for s in spans if s.name == "b")

    assert node_a.parent_run_id == root_id
    assert node_b.parent_run_id == root_id
    assert node_a.run_id != node_b.run_id  # full ids, never truncated
    assert node_a.parent_run_id != node_b.run_id
    assert node_b.parent_run_id != node_a.run_id


def test_r6_duplicate_start_for_one_run_id_yields_one_span() -> None:
    """FA4: the same on_chain_start delivered twice for one run id => one logical span."""
    handler = ZerothGovernanceCallbackHandler()
    run_id = uuid.uuid4()
    handler.on_chain_start({}, {}, run_id=run_id, parent_run_id=None)
    handler.on_chain_start({}, {}, run_id=run_id, parent_run_id=None)

    assert len(handler.open_spans) == 1
    assert handler.open_spans[0].run_id == str(run_id)


def test_r8_two_graphs_one_handler_do_not_cross_contaminate() -> None:
    """R8: two graphs driven concurrently through the SAME handler keep isolated trees.

    Trees are partitioned by their (distinct) correlation ids so the assertion is
    independent of thread scheduling; each partition must be an internally
    consistent single-rooted tree with no run ids shared across partitions.
    """
    signer = EnvHmacSigner(key_id="k", keys={"k": b"s"})
    handler = ZerothGovernanceCallbackHandler()
    g1 = govern_graph(_build_fanout(nested=False))
    g2 = govern_graph(_build_fanout(nested=False))
    # Force both wrappers to share ONE handler instance.
    g1._handler = handler
    g2._handler = handler

    def run(graph: Any, corr: str) -> None:
        graph.invoke({"text": corr}, config={"configurable": {"_zeroth": _token(signer, corr)}})

    threads = [
        threading.Thread(target=run, args=(g1, "CORR-A")),
        threading.Thread(target=run, args=(g2, "CORR-B")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    spans = handler.completed_spans
    part_a = [s for s in spans if s.correlation_id == "CORR-A"]
    part_b = [s for s in spans if s.correlation_id == "CORR-B"]
    ids_a = {s.run_id for s in part_a}
    ids_b = {s.run_id for s in part_b}

    assert ids_a and ids_b
    assert ids_a.isdisjoint(ids_b)  # no cross-run id collision
    for partition, ids in ((part_a, ids_a), (part_b, ids_b)):
        # Exactly one tree root, and every non-root parent points within the tree.
        assert len([s for s in partition if s.parent_run_id is None]) == 1
        for span in partition:
            if span.parent_run_id is not None:
                assert span.parent_run_id in ids


# --------------------------------------------------------------------------- #
# Ancestry shape and lifecycle.                                               #
# --------------------------------------------------------------------------- #


def test_r3_nested_run_reconstructs_exact_parent_child_edges() -> None:
    """FA1: a nested sub-runnable yields the exact tree root->{a,b,c}, a->inner."""
    governed = govern_graph(_build_fanout(nested=True))
    governed.invoke({"text": "hi"})
    spans = governed._handler.completed_spans
    assert len(spans) == 5  # root, a, inner_a, b, c

    root = next(s for s in spans if s.parent_run_id is None)
    node_a = next(s for s in spans if s.name == "a" and s.parent_run_id == root.run_id)
    node_b = next(s for s in spans if s.name == "b" and s.parent_run_id == root.run_id)
    node_c = next(s for s in spans if s.name == "c" and s.parent_run_id == root.run_id)

    inner = _children(spans, node_a.run_id)
    assert len(inner) == 1  # inner_a's parent is node a, NOT the root
    assert inner[0].parent_run_id == node_a.run_id
    assert inner[0].name == "inner_a"  # F2: named sub-runnable keeps ITS name, not node "a"
    # Exact edge set: root fans out to exactly {a, b, c}; a, b, c are otherwise leaves.
    assert {s.run_id for s in _children(spans, root.run_id)} == {
        node_a.run_id,
        node_b.run_id,
        node_c.run_id,
    }
    assert _children(spans, node_b.run_id) == []
    assert _children(spans, node_c.run_id) == []
    assert _children(spans, inner[0].run_id) == []


def test_r5_raising_node_yields_exactly_one_error_terminal() -> None:
    """FA3: a raising node produces exactly one error terminal span and no success."""
    governed = govern_graph(_build_error_graph())
    with pytest.raises(RuntimeError):
        governed.invoke({"text": "x"})
    spans = governed._handler.completed_spans

    boom_spans = [s for s in spans if s.name == "boom"]
    assert len(boom_spans) == 1
    assert boom_spans[0].status == "error"
    assert boom_spans[0].error_type == "RuntimeError"
    assert boom_spans[0].end is not None
    assert not [s for s in boom_spans if s.status == "ok"]
    # Every completed span is terminal exactly once (no run id appears twice).
    assert len({s.run_id for s in spans}) == len(spans)


# --------------------------------------------------------------------------- #
# Correlation: extract-only, unverified, propagated to every span.            #
# --------------------------------------------------------------------------- #


def test_r2_correlation_extraction_is_signature_independent() -> None:
    """R2: extraction reads the payload only; a garbage signature still resolves the id."""
    payload = base64.urlsafe_b64encode(json.dumps({"correlation_id": "CORR-Z"}).encode())
    forged = f"header.{payload.rstrip(b'=').decode()}.not-a-real-signature"
    assert _correlation_from_config({"configurable": {"_zeroth": forged}}) == "CORR-Z"


def test_r2_correlation_extraction_is_defensive() -> None:
    """R2: malformed / absent tokens resolve to None and never raise."""
    assert _correlation_from_config(None) is None
    assert _correlation_from_config({}) is None
    assert _correlation_from_config({"configurable": {}}) is None
    assert _correlation_from_config({"configurable": {"_zeroth": "one.two"}}) is None
    assert _correlation_from_config({"configurable": {"_zeroth": 123}}) is None
    assert _correlation_from_config({"configurable": {"_zeroth": "a.!!!.c"}}) is None


def test_r2_correlation_extraction_reads_a_real_gateway_token(signer: EnvHmacSigner) -> None:
    """R2: a genuine gateway-minted token resolves to its correlation id."""
    token = _token(signer, "CORR-REAL")
    assert _correlation_from_config({"configurable": {"_zeroth": token}}) == "CORR-REAL"


def test_r9_correlation_reaches_every_span_or_is_uniformly_absent(signer: EnvHmacSigner) -> None:
    """R9: with a token every span carries its correlation id; without one, all are None."""
    governed = govern_graph(_build_fanout(nested=True))
    governed.invoke({"text": "hi"}, config={"configurable": {"_zeroth": _token(signer, "CORR-42")}})
    with_token = governed._handler.completed_spans
    assert with_token
    assert all(s.correlation_id == "CORR-42" for s in with_token)

    bare = govern_graph(_build_fanout(nested=True))
    bare.invoke({"text": "hi"})
    without_token = bare._handler.completed_spans
    assert without_token
    assert all(s.correlation_id is None for s in without_token)


# --------------------------------------------------------------------------- #
# Equivalence with correlation ON and econ ON (the ZER-2 F3 lesson).          #
# --------------------------------------------------------------------------- #


def test_r10_invoke_stream_astream_equivalent_with_correlation_and_econ(
    signer: EnvHmacSigner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R10: correlation injection + econ leave results and stream order unchanged."""
    runtime = get_runtime()
    runtime.config.enabled = True
    monkeypatch.setattr(runtime.transport, "enqueue_execution", lambda event: None)

    graph = _build_router()
    config = {"configurable": {"_zeroth": _token(signer, "CORR-EQ")}}

    direct = graph.invoke({"text": "echo"}, config=dict(config))
    wrapped = govern_graph(graph).invoke({"text": "echo"}, config=dict(config))
    assert json.dumps(wrapped, sort_keys=True) == json.dumps(direct, sort_keys=True)

    direct_stream = list(graph.stream({"text": "emit"}, stream_mode="custom", config=dict(config)))
    wrapped_stream = list(
        govern_graph(graph).stream({"text": "emit"}, stream_mode="custom", config=dict(config))
    )
    assert wrapped_stream == direct_stream
    assert [chunk["seq"] for chunk in wrapped_stream] == [1, 2]

    direct_astream = asyncio.run(
        _drain(graph.astream({"text": "emit"}, stream_mode="custom", config=dict(config)))
    )
    wrapped_astream = asyncio.run(
        _drain(
            govern_graph(graph).astream({"text": "emit"}, stream_mode="custom", config=dict(config))
        )
    )
    assert wrapped_astream == direct_astream


def test_r10_cancellation_equivalent_with_correlation(signer: EnvHmacSigner) -> None:
    """R10: correlation injection does not change ainvoke cancellation behaviour."""
    graph = _build_router()
    governed = govern_graph(graph)
    config = {"configurable": {"_zeroth": _token(signer, "CORR-CX")}}

    async def cancel(target: Any) -> None:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(target.ainvoke({"text": "cancel"}, config=dict(config)), 0.2)

    asyncio.run(cancel(graph))
    asyncio.run(cancel(governed))
