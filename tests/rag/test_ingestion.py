"""Tests for RAG ingestion: chunking and document ingestion (RAG-02)."""

from __future__ import annotations

import pytest
from zeroth.core.governed.memory.models import MemoryScope

from zeroth.core.rag import IngestionReport, SourceDocument, chunk_text, ingest_documents


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
