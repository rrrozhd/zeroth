"""Tests for RAG ingestion: chunking and document ingestion (RAG-02)."""

from __future__ import annotations

import pytest

from zeroth.integrations.memory.governed.models import MemoryScope
from zeroth.integrations.rag import (
    IngestionReport,
    IngestionRolledBackError,
    PartialIngestionError,
    SourceDocument,
    chunk_text,
    ingest_documents,
)


def test_chunk_text_basic_no_overlap() -> None:
    assert chunk_text("abcdefghij", chunk_size=5, overlap=0) == ["abcde", "fghij"]


def test_chunk_text_overlap_shares_characters() -> None:
    chunks = chunk_text("abcdefghij", chunk_size=5, overlap=2)
    assert chunks[0] == "abcde"
    assert chunks[1] == "defgh"
    # consecutive chunks share `overlap` characters
    assert chunks[0][-2:] == chunks[1][:2]


def test_chunk_text_empty_returns_empty() -> None:
    assert chunk_text("   ") == []


def test_chunk_text_validates_sizes() -> None:
    with pytest.raises(ValueError):
        chunk_text("x", chunk_size=0)
    with pytest.raises(ValueError):
        chunk_text("x", chunk_size=5, overlap=5)  # overlap must be < chunk_size


class _FakeVectorStore:
    """A connector with *visible* state: ``store`` is what a reader would see.

    ``store`` and ``writes`` are deliberately separate. ``store`` is the index
    a retrieval would search; ``writes`` is only a log of attempts. A test that
    asserts on ``writes`` says what the ingest *did*; a test that asserts on
    ``store`` says what it *left*, which is what R15-A07-23 is about.

    Modelled on the real backends: ``delete`` raises ``KeyError`` for a key that
    is not there (``chroma_connector.py:275``, ``pgvector_connector.py:211``)
    and ``write`` overwrites in place.
    """

    def __init__(
        self,
        *,
        fail_write_after: int | None = None,
        half_apply_write: int | None = None,
        undeletable: frozenset[str] = frozenset(),
    ) -> None:
        self.store: dict[str, object] = {}
        self.writes: list[tuple[str, object, MemoryScope]] = []
        self.deletes: list[str] = []
        self._fail_write_after = fail_write_after
        self._half_apply_write = half_apply_write
        self._undeletable = undeletable

    async def write(self, key, value, scope, *, target=None):  # noqa: ANN001
        attempt = len(self.writes)
        if attempt == self._half_apply_write:
            # The value lands and *then* the call fails -- a lost ack. The
            # caller cannot tell this apart from a write that never applied.
            self.store[key] = value
            raise ConnectionError("connection lost after the write applied")
        if self._fail_write_after is not None and attempt >= self._fail_write_after:
            raise ConnectionError("vector backend went away")
        self.writes.append((key, value, scope))
        self.store[key] = value

    async def delete(self, key, scope, *, target=None):  # noqa: ANN001
        self.deletes.append(key)
        if key in self._undeletable:
            raise ConnectionError("vector backend still away")
        if key not in self.store:
            raise KeyError(key)
        del self.store[key]


def _three_documents() -> list[SourceDocument]:
    return [
        SourceDocument(source_id="a", text="abcdefghij"),  # 2 chunks
        SourceDocument(source_id="b", text="klmnopqrst"),  # 2 chunks
        SourceDocument(source_id="c", text="uvwxy"),  # 1 chunk
    ]


@pytest.mark.asyncio
async def test_ingest_documents_chunks_and_writes_with_source_keys() -> None:
    store = _FakeVectorStore()
    docs = [
        SourceDocument(source_id="guide", text="abcdefghij"),
        SourceDocument(source_id="faq", text="xyz"),
    ]
    report = await ingest_documents(store, docs, MemoryScope.SHARED, chunk_size=5, overlap=0)

    assert isinstance(report, IngestionReport)
    assert report.documents == 2
    assert report.chunks_written == 3  # "abcdefghij" -> 2 chunks, "xyz" -> 1
    keys = [w[0] for w in store.writes]
    assert keys == ["guide#0", "guide#1", "faq#0"]  # source attribution travels via the key
    assert store.writes[0][1] == "abcde"  # chunk text is the written value (connector embeds it)
    assert all(w[2] is MemoryScope.SHARED for w in store.writes)
    assert store.deletes == []  # a successful ingest sweeps nothing


@pytest.mark.asyncio
async def test_ingest_empty_document_writes_nothing() -> None:
    store = _FakeVectorStore()
    report = await ingest_documents(
        store, [SourceDocument(source_id="empty", text="  ")], MemoryScope.SHARED
    )
    assert report.chunks_written == 0
    assert store.writes == []


@pytest.mark.asyncio
async def test_a_failed_ingest_leaves_none_of_its_chunks_in_the_index() -> None:
    """R15-A07-23: an ingest that fails leaves no partial ingestion.

    Stated the way the requirement states it -- about what a reader of the
    backend can see afterwards, not about what the raised error says. The
    failure is injected after chunk 4 of 5; afterwards no key this call wrote
    may still be visible.
    """
    store = _FakeVectorStore(fail_write_after=4)

    with pytest.raises(Exception):  # noqa: B017 - the property is about the store
        await ingest_documents(
            store, _three_documents(), MemoryScope.SHARED, chunk_size=5, overlap=0
        )

    assert store.writes, "the failure must be injected mid-run, or the test proves nothing"
    assert store.store == {}, f"chunks left indexed by a failed ingest: {sorted(store.store)}"


@pytest.mark.asyncio
async def test_the_sweep_covers_the_write_that_failed_as_well() -> None:
    """A write can apply and *then* fail; the key is swept on that suspicion.

    Absence is the postcondition, so the key of the failed write is deleted
    like the rest -- the alternative is trusting an error that cannot be
    trusted about whether its value landed.
    """
    store = _FakeVectorStore(half_apply_write=2)

    with pytest.raises(IngestionRolledBackError):
        await ingest_documents(
            store, _three_documents(), MemoryScope.SHARED, chunk_size=5, overlap=0
        )

    assert store.deletes == ["b#0", "a#1", "a#0"]  # b#0 is the write that failed
    assert store.store == {}


@pytest.mark.asyncio
async def test_a_rolled_back_ingest_says_nothing_is_indexed() -> None:
    """Whatever it raises has to tell the caller what is in the index.

    Here the honest answer is "nothing", and the error says exactly that
    instead of leaving the caller to query for keys it would have to guess.
    """
    store = _FakeVectorStore(fail_write_after=4)

    with pytest.raises(IngestionRolledBackError) as raised:
        await ingest_documents(
            store, _three_documents(), MemoryScope.SHARED, chunk_size=5, overlap=0
        )

    error = raised.value
    assert isinstance(error.__cause__, ConnectionError)
    assert error.report == IngestionReport(documents=0, chunks_written=0)
    assert error.report.chunks_written == len(store.store)
    assert not isinstance(error, PartialIngestionError)  # nothing partial survived


@pytest.mark.asyncio
async def test_a_rolled_back_ingest_names_the_chunk_that_broke_it() -> None:
    """The cursor names the failed chunk -- as a diagnostic, not a resume point."""
    store = _FakeVectorStore(fail_write_after=3)

    with pytest.raises(IngestionRolledBackError) as raised:
        await ingest_documents(
            store, _three_documents(), MemoryScope.SHARED, chunk_size=5, overlap=0
        )

    cursor = raised.value.cursor
    assert (cursor.document_index, cursor.source_id, cursor.chunk_index) == (1, "b", 1)
    assert store.store == {}


@pytest.mark.asyncio
async def test_a_retry_after_rollback_re_offers_the_whole_sequence() -> None:
    """Nothing survived the first attempt, so the retry starts from the top.

    This is what replaced resuming at ``cursor.document_index``: slicing the
    sequence there would now skip documents that were rolled back and index
    only the tail.
    """
    documents = _three_documents()
    with pytest.raises(IngestionRolledBackError):
        await ingest_documents(
            _FakeVectorStore(fail_write_after=3),
            documents,
            MemoryScope.SHARED,
            chunk_size=5,
            overlap=0,
        )

    retried = _FakeVectorStore()
    report = await ingest_documents(retried, documents, MemoryScope.SHARED, chunk_size=5, overlap=0)

    assert report == IngestionReport(documents=3, chunks_written=5)
    assert sorted(retried.store) == ["a#0", "a#1", "b#0", "b#1", "c#0"]


@pytest.mark.asyncio
async def test_a_sweep_that_fails_surfaces_the_keys_it_could_not_remove() -> None:
    """Compensation can fail too, and then the residue is named, not swallowed.

    Two deletes are refused. The sweep still attempts every key (a failure
    partway must not orphan the rest), and what it could not remove is reported
    key by key so a reconciler can finish the job.
    """
    store = _FakeVectorStore(fail_write_after=4, undeletable=frozenset({"a#1", "b#0"}))

    with pytest.raises(PartialIngestionError) as raised:
        await ingest_documents(
            store, _three_documents(), MemoryScope.SHARED, chunk_size=5, overlap=0
        )

    error = raised.value
    assert isinstance(error.__cause__, ConnectionError)  # the write failure, not a delete failure
    assert error.orphaned_keys == ("b#0", "a#1")
    assert sorted(store.store) == ["a#1", "b#0"]  # exactly what the error claims is left
    assert error.report == IngestionReport(documents=2, chunks_written=2)
    assert error.report.chunks_written == len(store.store)
    assert [type(exc) for exc in error.cleanup_errors] == [ConnectionError, ConnectionError]
    # every key was attempted exactly once, refusals included
    assert store.deletes == ["c#0", "b#1", "b#0", "a#1", "a#0"]
    assert len(store.deletes) == len(set(store.deletes))


@pytest.mark.asyncio
async def test_a_rollback_touches_only_the_keys_this_ingest_wrote() -> None:
    """Re-ingesting an indexed document: the sweep stays inside its own keys.

    ``a`` is already indexed with four chunks of older text. The new text is
    three chunks and the third write fails. Every key this call wrote (or tried
    to) goes; ``a#3``, which it never reached, keeps its old value. The
    rewritten keys are deleted rather than restored -- ``write`` carries no
    metadata and entry timestamps are backend-assigned, so a pre-image cannot
    be put back faithfully, and a retry rewrites the document whole anyway.
    """
    store = _FakeVectorStore(fail_write_after=2)
    store.store.update({"a#0": "OLD0", "a#1": "OLD1", "a#2": "OLD2", "a#3": "OLD3"})

    with pytest.raises(IngestionRolledBackError):
        await ingest_documents(
            store,
            [SourceDocument(source_id="a", text="uvwxyz12345")],  # 3 chunks of 5
            MemoryScope.SHARED,
            chunk_size=5,
            overlap=0,
        )

    assert store.store == {"a#3": "OLD3"}


@pytest.mark.asyncio
async def test_an_ingest_refuses_a_connector_it_could_not_undo() -> None:
    """No ``delete``, no ingest: the promise is refused up front, not broken later."""

    class _WriteOnlyConnector:
        def __init__(self) -> None:
            self.writes: list[str] = []

        async def write(self, key, value, scope, *, target=None):  # noqa: ANN001
            self.writes.append(key)

    connector = _WriteOnlyConnector()
    with pytest.raises(TypeError, match="delete"):
        await ingest_documents(
            connector, _three_documents(), MemoryScope.SHARED, chunk_size=5, overlap=0
        )

    assert connector.writes == []  # refused before the first write
