"""Root / orphan classification tests for the governance handler (ZER-3, FA5).

Companion to ``test_callback_ancestry.py`` (split out to keep each file within
the 400-line ceiling). Same gate: the ``langgraph_conformance`` marker
(deselected by default) -- run with
``uv run pytest -o addopts= -q tests/integrations/langgraph/``.

Orphan is a *read-time* determination: a span is an orphan iff its
``parent_run_id`` names a run id that no start was ever observed for. Every
``parent_run_id is None`` start is a root (never an orphan), so many roots --
concurrent or sequential -- coexist cleanly on one shared handler. These tests
drive the handler callbacks directly (no compiled graph) to pin the rule and its
determination point.
"""

from __future__ import annotations

import threading
import uuid

import pytest

pytest.importorskip("langgraph", reason="requires the gateway-conformance dependency group")

from zeroth.integrations.langgraph import ZerothGovernanceCallbackHandler

pytestmark = pytest.mark.langgraph_conformance


def test_r7_dangling_parent_is_orphaned_not_reparented() -> None:
    """FA5: a child naming a never-observed parent is an orphan, not reparented to a root.

    Rewritten from the old Reading-A premise (a ``parent=None`` start became an
    orphan when a root was already open). Under the correct rule a ``parent=None``
    start is a *root*; the orphan is instead a dangling *non-None* parent
    reference, and it is never silently attached to the open root.
    """
    handler = ZerothGovernanceCallbackHandler()
    root_id = uuid.uuid4()
    handler.on_chain_start({}, {}, run_id=root_id, parent_run_id=None)  # a genuine root

    ghost_id = uuid.uuid4()  # a parent whose start is never delivered
    child_id = uuid.uuid4()
    handler.on_chain_start({}, {}, run_id=child_id, parent_run_id=ghost_id)

    spans = {s.run_id: s for s in handler.open_spans}
    root = spans[str(root_id)]
    child = spans[str(child_id)]

    assert root.status == "running"  # parent=None => root, never orphan
    assert root.parent_run_id is None
    assert child.status == "orphan"  # dangling parent => orphan at read time
    assert child.parent_run_id == str(ghost_id)  # kept verbatim, NOT reparented
    assert child.parent_run_id != str(root_id)


def test_two_concurrent_roots_through_one_handler_are_both_roots() -> None:
    """FA5 defect falsifier: two concurrent top-level runs on ONE handler are BOTH roots.

    A ``threading.Barrier`` holds both ``parent=None`` starts open simultaneously
    before either ends, so the Reading-A "only one root may be open" rule would
    mislabel exactly one as ``orphan`` (scheduling-dependent ancestry corruption).
    The correct rule yields two roots and zero orphans, whatever the interleaving.
    """
    handler = ZerothGovernanceCallbackHandler()
    both_started = threading.Barrier(2)

    def run_root() -> None:
        run_id = uuid.uuid4()
        handler.on_chain_start({}, {}, run_id=run_id, parent_run_id=None)
        both_started.wait(timeout=5)  # both roots are open at once here
        handler.on_chain_end({}, run_id=run_id)

    threads = [threading.Thread(target=run_root) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    spans = handler.completed_spans
    assert len(spans) == 2
    assert sum(1 for s in spans if s.parent_run_id is None) == 2  # both are roots
    assert sum(1 for s in spans if s.status == "orphan") == 0  # none mislabeled


def test_child_start_before_parent_start_resolves_to_real_parent() -> None:
    """FA5: an out-of-order child (delivered before its parent) resolves to the real parent.

    Delivering the child ``on_chain_start`` first must NOT freeze it as an orphan:
    by read time the parent's start has arrived, so the read-time determination
    sees the parent and the child is an ordinary (non-orphan) descendant.
    """
    handler = ZerothGovernanceCallbackHandler()
    parent_id = uuid.uuid4()
    child_id = uuid.uuid4()

    handler.on_chain_start({}, {}, run_id=child_id, parent_run_id=parent_id)  # child FIRST
    handler.on_chain_start({}, {}, run_id=parent_id, parent_run_id=None)  # parent AFTER

    spans = {s.run_id: s for s in handler.open_spans}
    child = spans[str(child_id)]
    assert child.status == "running"  # NOT orphan: parent observed by read time
    assert child.parent_run_id == str(parent_id)  # resolves to the real parent
    assert spans[str(parent_id)].parent_run_id is None  # the parent is a root


def test_dangling_parent_orphan_survives_terminal_in_completed_spans() -> None:
    """FA5: a dangling-parent span reads as ``orphan`` even after it terminates ``ok``.

    The determination is made at read time against every observed run id, so it
    holds in ``completed_spans`` after a terminal event -- the lifecycle ``ok`` is
    overridden and the dangling parent is still not reparented.
    """
    handler = ZerothGovernanceCallbackHandler()
    ghost_id = uuid.uuid4()  # never observed
    child_id = uuid.uuid4()

    handler.on_chain_start({}, {}, run_id=child_id, parent_run_id=ghost_id)
    handler.on_chain_end({}, run_id=child_id)  # lifecycle terminal: ok

    assert handler.open_spans == []
    completed = handler.completed_spans
    assert len(completed) == 1
    span = completed[0]
    assert span.status == "orphan"  # overrides the ok lifecycle at read time
    assert span.end is not None  # it really did terminate
    assert span.parent_run_id == str(ghost_id)  # not reparented
