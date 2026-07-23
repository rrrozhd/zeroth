"""Audit-1 hardening tests for ZER-3 causal-ancestry capture.

Companion to ``test_callback_ancestry{,_orphans}.py`` (split out to keep each
file within the 400-line ceiling). Same gate: the ``langgraph_conformance``
marker (deselected by default) -- run with
``uv run pytest -o addopts= -q tests/integrations/langgraph/``.

Each test pins one Codex audit finding:

* F1 -- an untrusted caller cannot forge the gateway correlation id.
* F3 -- a deeply nested / oversized ``_zeroth`` token is a rejected, not a DoS.
* F2 -- a span's name comes from the callback / serialized identity, not only the
  enclosing LangGraph node.
* F4 -- a returned span's metadata is immutable.
* F5 -- the LLM callbacks expose the upstream lc-core ``tags`` parameter.
* F6 -- start dedup keys on the FULL run id, never a shared prefix.
* F7 -- concurrent reads during writes never race the lock-guarded accessors.
"""

from __future__ import annotations

import base64
import inspect
import json
import threading
import uuid
from typing import Any, TypedDict

import pytest

pytest.importorskip("langgraph", reason="requires the gateway-conformance dependency group")

from langgraph.graph import END, START, StateGraph

from zeroth.econ.instrumentation.runtime import get_runtime
from zeroth.integrations.langgraph import ZerothGovernanceCallbackHandler, govern_graph
from zeroth.integrations.langgraph._correlation import _correlation_from_config

pytestmark = pytest.mark.langgraph_conformance


# --------------------------------------------------------------------------- #
# Helpers.                                                                     #
# --------------------------------------------------------------------------- #


class _State(TypedDict, total=False):
    text: str
    out: str


def _build_graph() -> Any:
    builder = StateGraph(_State)
    builder.add_node("n", lambda _s: {"out": "ok"})
    builder.add_edge(START, "n")
    builder.add_edge("n", END)
    return builder.compile()


def _payload_token(correlation_id: str) -> str:
    """A token whose payload segment decodes to a correlation id (signature ignored)."""
    payload = base64.urlsafe_b64encode(json.dumps({"correlation_id": correlation_id}).encode())
    return f"header.{payload.rstrip(b'=').decode()}.signature"


def _nested_token(depth: int) -> str:
    """A token whose payload is a JSON array nested ``depth`` levels deep (a DoS probe)."""
    payload = ("[" * depth + "]" * depth).encode()
    segment = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
    return f"header.{segment}.signature"


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


# --------------------------------------------------------------------------- #
# F1 -- correlation trust boundary (security): no forged correlation.          #
# --------------------------------------------------------------------------- #


def test_f1_forged_correlation_without_token_is_stripped_to_none() -> None:
    """F1: a caller-forged ``metadata`` correlation with NO ``_zeroth`` yields None spans."""
    governed = govern_graph(_build_graph())
    governed.invoke({"text": "hi"}, config={"metadata": {"zeroth_correlation_id": "FORGED"}})
    spans = governed._handler.completed_spans
    assert spans
    assert all(s.correlation_id is None for s in spans)
    assert all(s.correlation_id != "FORGED" for s in spans)


def test_f1_valid_token_wins_over_forged_metadata() -> None:
    """F1: a real extracted correlation is injected; a caller 'FORGED' value never leaks."""
    governed = govern_graph(_build_graph())
    config = {
        "metadata": {"zeroth_correlation_id": "FORGED"},
        "configurable": {"_zeroth": _payload_token("CORR-REAL")},
    }
    governed.invoke({"text": "hi"}, config=config)
    spans = governed._handler.completed_spans
    assert spans
    assert all(s.correlation_id == "CORR-REAL" for s in spans)
    assert all(s.correlation_id != "FORGED" for s in spans)


# --------------------------------------------------------------------------- #
# F3 -- token parsing is a bounded, non-raising DoS guard.                     #
# --------------------------------------------------------------------------- #


def test_f3_deeply_nested_token_returns_none_without_raising() -> None:
    """F3: a deeply nested payload (a RecursionError DoS vector) resolves to None, no raise."""
    token = _nested_token(15000)  # deeper than json's C-scanner tolerates AND over the size cap
    assert _correlation_from_config({"configurable": {"_zeroth": token}}) is None


def test_f3_oversized_token_returns_none() -> None:
    """F3: an oversized token is rejected by length before any decode work is done."""
    token = "header." + "A" * 20000 + ".signature"
    assert _correlation_from_config({"configurable": {"_zeroth": token}}) is None


def test_f3_governed_invoke_survives_dos_token_with_none_correlation() -> None:
    """F3: governing a graph and invoking with a DoS token succeeds; every span carries None."""
    governed = govern_graph(_build_graph())
    governed.invoke({"text": "hi"}, config={"configurable": {"_zeroth": _nested_token(15000)}})
    spans = governed._handler.completed_spans
    assert spans
    assert all(s.correlation_id is None for s in spans)


# --------------------------------------------------------------------------- #
# F2 -- span naming resolves callback / serialized identity, not only the node.#
# --------------------------------------------------------------------------- #


def test_f2_tool_span_uses_serialized_name() -> None:
    """F2: a tool span takes its name from ``serialized['name']`` (no langgraph_node present)."""
    handler = ZerothGovernanceCallbackHandler()
    handler.on_tool_start({"name": "my_tool"}, "input", run_id=uuid.uuid4(), parent_run_id=None)
    span = handler.open_spans[0]
    assert span.kind == "tool"
    assert span.name == "my_tool"


def test_f2_chat_model_span_uses_serialized_name() -> None:
    """F2: a chat-model span takes its name from ``serialized['name']``."""
    handler = ZerothGovernanceCallbackHandler()
    handler.on_chat_model_start({"name": "FakeChat"}, [[]], run_id=uuid.uuid4(), parent_run_id=None)
    span = handler.open_spans[0]
    assert span.kind == "chat_model"
    assert span.name == "FakeChat"


def test_f2_callback_name_kwarg_beats_serialized_and_node() -> None:
    """F2: the explicit callback ``name`` kwarg wins over serialized name and langgraph_node."""
    handler = ZerothGovernanceCallbackHandler()
    handler.on_chain_start(
        {"name": "serialized_name"},
        {},
        run_id=uuid.uuid4(),
        parent_run_id=None,
        metadata={"langgraph_node": "enclosing_node"},
        name="explicit_name",
    )
    assert handler.open_spans[0].name == "explicit_name"


# --------------------------------------------------------------------------- #
# F4 -- returned span metadata is immutable (whitelist cannot be defeated).    #
# --------------------------------------------------------------------------- #


def test_f4_returned_span_metadata_is_immutable() -> None:
    """F4: a consumer cannot mutate a returned span's metadata, and the whitelist holds."""
    handler = ZerothGovernanceCallbackHandler()
    handler.on_chain_start(
        {},
        {},
        run_id=uuid.uuid4(),
        parent_run_id=None,
        metadata={"langgraph_node": "n", "thread_id": "t", "not_whitelisted": "secret"},
    )
    span = handler.open_spans[0]
    assert dict(span.metadata) == {"langgraph_node": "n", "thread_id": "t"}
    with pytest.raises(TypeError):
        span.metadata["langgraph_node"] = "hacked"  # type: ignore[index]
    with pytest.raises(TypeError):
        span.metadata["injected"] = "x"  # type: ignore[index]
    # A later snapshot is uncorrupted by the attempted mutations.
    assert handler.open_spans[0].metadata["langgraph_node"] == "n"


# --------------------------------------------------------------------------- #
# F5 -- LLM callbacks mirror the lc-core 1.5.0 signatures (``tags`` present).   #
# --------------------------------------------------------------------------- #


def test_f5_llm_callbacks_expose_upstream_tags_parameter() -> None:
    """F5: on_llm_end / on_llm_error carry the upstream ``tags`` param; behaviour unchanged."""
    handler = ZerothGovernanceCallbackHandler()
    assert "tags" in inspect.signature(handler.on_llm_end).parameters
    assert "tags" in inspect.signature(handler.on_llm_error).parameters

    rid = uuid.uuid4()
    handler.on_chain_start({}, {}, run_id=rid, parent_run_id=None)
    handler.on_llm_end(None, run_id=rid, tags=["x"])  # accepts tags, still terminates ok
    assert handler.completed_spans[0].status == "ok"


# --------------------------------------------------------------------------- #
# F6 -- dedup keys on the FULL run id, never a shared prefix.                  #
# --------------------------------------------------------------------------- #


def test_f6_dedup_keys_on_full_run_id_not_a_prefix() -> None:
    """F6: same-id starts collapse, but two ids sharing a long prefix both produce spans."""
    handler = ZerothGovernanceCallbackHandler()
    # Same full id delivered twice => exactly one span (the original FA4 guarantee).
    same = uuid.uuid4()
    handler.on_chain_start({}, {}, run_id=same, parent_run_id=None)
    handler.on_chain_start({}, {}, run_id=same, parent_run_id=None)
    assert len([s for s in handler.open_spans if s.run_id == str(same)]) == 1

    # Two DISTINCT ids sharing a 35-char prefix, as UUIDv7 ids share their time prefix.
    id1 = uuid.UUID("0190d8e2-7f3a-7c9a-8b1a-000000000001")
    id2 = uuid.UUID("0190d8e2-7f3a-7c9a-8b1a-000000000002")
    assert str(id1)[:35] == str(id2)[:35]  # long shared prefix
    assert id1 != id2
    handler.on_chain_start({}, {}, run_id=id1, parent_run_id=None)
    handler.on_chain_start({}, {}, run_id=id2, parent_run_id=None)
    ids = {s.run_id for s in handler.open_spans}
    assert {str(id1), str(id2)} <= ids  # BOTH kept: dedup uses the full id, a prefix would collapse


# --------------------------------------------------------------------------- #
# F7 -- live reads during interleaved writes never race the accessors.         #
# --------------------------------------------------------------------------- #


_RACE_BATCH = 4  # spans a writer holds open at once, so the accessor iterates a moving map


def _race_writer(
    handler: Any, start: threading.Barrier, errors: list[Exception], rounds: int
) -> None:
    """Open then complete ``rounds`` batches of spans, recording any exception."""
    try:
        start.wait(timeout=10)
        for _ in range(rounds):
            rids = [uuid.uuid4() for _ in range(_RACE_BATCH)]
            for rid in rids:
                handler.on_chain_start(
                    {}, {}, run_id=rid, parent_run_id=None, metadata={"langgraph_node": "n"}
                )
            for rid in rids:
                handler.on_chain_end({}, run_id=rid)
    except Exception as exc:  # surfaced via `errors`, asserted in the main thread
        errors.append(exc)


def _race_reader(
    handler: Any, start: threading.Barrier, stop: threading.Event, errors: list[Exception]
) -> None:
    """Repeatedly read and iterate both accessors until ``stop``, recording any exception."""
    try:
        start.wait(timeout=10)
        while not stop.is_set():
            for span in handler.open_spans:
                _ = (span.status, span.run_id, span.parent_run_id)
            for span in handler.completed_spans:
                assert span.end is not None
                assert span.status in {"ok", "error", "orphan"}
    except Exception as exc:  # surfaced via `errors`, asserted in the main thread
        errors.append(exc)


def test_f7_concurrent_reads_during_writes_never_race() -> None:
    """F7: readers hammering the accessors while writers interleave never race or corrupt.

    Barrier-synchronised writers open and complete many spans while several readers
    repeatedly call ``open_spans`` / ``completed_spans`` and iterate the results. An
    unlocked read (``_resolve`` racing ``_seen`` / the span map) would surface as a
    'dictionary changed size during iteration' RuntimeError or an inconsistent
    snapshot; the lock-guarded accessors must yield neither.
    """
    handler = ZerothGovernanceCallbackHandler()
    writers_n, readers_n, rounds = 4, 3, 300
    start = threading.Barrier(writers_n + readers_n)
    errors: list[Exception] = []
    stop = threading.Event()

    writers = [
        threading.Thread(target=_race_writer, args=(handler, start, errors, rounds))
        for _ in range(writers_n)
    ]
    readers = [
        threading.Thread(target=_race_reader, args=(handler, start, stop, errors))
        for _ in range(readers_n)
    ]
    for thread in writers + readers:
        thread.start()
    for thread in writers:
        thread.join()
    stop.set()
    for thread in readers:
        thread.join()

    assert not errors
    assert len(handler.completed_spans) == writers_n * rounds * _RACE_BATCH
