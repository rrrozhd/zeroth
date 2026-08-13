"""Tests for RAG ingestion: chunking and document ingestion (RAG-02)."""

from __future__ import annotations

import pytest

from zeroth.integrations.memory.governed.models import MemoryScope
from zeroth.integrations.rag import IngestionReport, SourceDocument, chunk_text, ingest_documents


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


class _RecordingWriter:
    """Captures connector.write calls (stands in for a vector connector)."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, object, MemoryScope]] = []

    async def write(self, key, value, scope, *, target=None):  # noqa: ANN001
        self.writes.append((key, value, scope))


@pytest.mark.asyncio
async def test_ingest_documents_chunks_and_writes_with_source_keys() -> None:
    writer = _RecordingWriter()
    docs = [
        SourceDocument(source_id="guide", text="abcdefghij"),
        SourceDocument(source_id="faq", text="xyz"),
    ]
    report = await ingest_documents(writer, docs, MemoryScope.SHARED, chunk_size=5, overlap=0)

    assert isinstance(report, IngestionReport)
    assert report.documents == 2
    assert report.chunks_written == 3  # "abcdefghij" -> 2 chunks, "xyz" -> 1
    keys = [w[0] for w in writer.writes]
    assert keys == ["guide#0", "guide#1", "faq#0"]  # source attribution travels via the key
    assert writer.writes[0][1] == "abcde"  # chunk text is the written value (connector embeds it)
    assert all(w[2] is MemoryScope.SHARED for w in writer.writes)


@pytest.mark.asyncio
async def test_ingest_empty_document_writes_nothing() -> None:
    writer = _RecordingWriter()
    report = await ingest_documents(
        writer, [SourceDocument(source_id="empty", text="  ")], MemoryScope.SHARED
    )
    assert report.chunks_written == 0
    assert writer.writes == []


class _FailingWriter:
    """A connector whose write starts refusing after ``fail_after`` chunks."""

    def __init__(self, fail_after: int) -> None:
        self.writes: list[tuple[str, object, MemoryScope]] = []
        self._fail_after = fail_after

    async def write(self, key, value, scope, *, target=None):  # noqa: ANN001
        if len(self.writes) >= self._fail_after:
            raise ConnectionError("vector backend went away")
        self.writes.append((key, value, scope))


def _three_documents() -> list[SourceDocument]:
    return [
        SourceDocument(source_id="a", text="abcdefghij"),  # 2 chunks
        SourceDocument(source_id="b", text="klmnopqrst"),  # 2 chunks
        SourceDocument(source_id="c", text="uvwxy"),  # 1 chunk
    ]


@pytest.mark.asyncio
async def test_a_failed_ingest_does_not_hide_what_it_already_wrote() -> None:
    """The property, stated without naming the mechanism that carries it.

    Whatever ``ingest_documents`` raises, it has to tell the caller how much is
    already in the index -- otherwise the only way to find out is to query the
    backend for keys the caller would have to guess.
    """
    writer = _FailingWriter(fail_after=4)

    with pytest.raises(Exception) as raised:  # noqa: B017 - the type is the point
        await ingest_documents(
            writer, _three_documents(), MemoryScope.SHARED, chunk_size=5, overlap=0
        )

    landed = getattr(raised.value, "report", None)
    assert landed is not None, "a failed ingest reports nothing about what it wrote"
    assert landed.chunks_written == len(writer.writes)


@pytest.mark.asyncio
async def test_a_failed_ingest_reports_exactly_what_landed() -> None:
    """A07-23: a partial ingest is reportable, because it cannot be prevented.

    The sink is a vector/KV backend with no transaction to join, so a failure
    on document 3 leaves documents 1 and 2 durably indexed. ``ingest_documents``
    used to let the connector's own error escape and returned its report only on
    the success path, so the caller was left with chunks in the index and no
    record of which ones.
    """
    from zeroth.integrations.rag import PartialIngestionError

    writer = _FailingWriter(fail_after=4)

    with pytest.raises(PartialIngestionError) as raised:
        await ingest_documents(
            writer, _three_documents(), MemoryScope.SHARED, chunk_size=5, overlap=0
        )

    error = raised.value
    assert isinstance(error.__cause__, ConnectionError)
    assert [key for key, _, _ in writer.writes] == ["a#0", "a#1", "b#0", "b#1"]
    # Only whole documents count as ingested; the one that failed does not.
    assert error.report == IngestionReport(documents=2, chunks_written=4)
    assert error.cursor.document_index == 2
    assert error.cursor.source_id == "c"
    assert error.cursor.chunk_index == 0


@pytest.mark.asyncio
async def test_a_partial_ingest_reports_a_document_that_failed_midway() -> None:
    """The cursor names the chunk that failed, not just the document."""
    from zeroth.integrations.rag import PartialIngestionError

    writer = _FailingWriter(fail_after=3)

    with pytest.raises(PartialIngestionError) as raised:
        await ingest_documents(
            writer, _three_documents(), MemoryScope.SHARED, chunk_size=5, overlap=0
        )

    error = raised.value
    assert error.report == IngestionReport(documents=1, chunks_written=3)
    assert error.cursor.document_index == 1
    assert error.cursor.source_id == "b"
    assert error.cursor.chunk_index == 1


@pytest.mark.asyncio
async def test_a_reported_partial_ingest_resumes_from_its_cursor() -> None:
    """Re-writes are idempotent per key, so the cursor is a resume point.

    Resuming means re-offering ``documents[cursor.document_index:]``: the
    partially-written document is rewritten whole, which re-keys the same
    ``{source_id}#{index}`` cells.
    """
    from zeroth.integrations.rag import PartialIngestionError

    documents = _three_documents()
    with pytest.raises(PartialIngestionError) as raised:
        await ingest_documents(
            _FailingWriter(fail_after=3), documents, MemoryScope.SHARED, chunk_size=5, overlap=0
        )

    resumed = _RecordingWriter()
    report = await ingest_documents(
        resumed,
        documents[raised.value.cursor.document_index :],
        MemoryScope.SHARED,
        chunk_size=5,
        overlap=0,
    )

    assert report == IngestionReport(documents=2, chunks_written=3)
    assert [key for key, _, _ in resumed.writes] == ["b#0", "b#1", "c#0"]
