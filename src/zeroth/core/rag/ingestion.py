"""RAG ingestion: chunk documents and write them to a memory connector (RAG-02).

Splits source text into overlapping character chunks and writes each chunk to a
memory connector via its ``write(key, value, scope)`` method. Embedding is
*delegated to the connector* (chroma / pgvector embed on write) — this module does
not embed. Each chunk is keyed ``{source_id}#{index}`` so a RetrievalNode can later
attribute the retrieved chunk back to its source document.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from zeroth.core.governed.memory.models import MemoryScope


@dataclass(frozen=True)
class SourceDocument:
    """A document to ingest: a stable source id and its text."""

    source_id: str
    text: str


@dataclass(frozen=True)
class IngestionReport:
    """Summary of one ingestion run."""

    documents: int
    chunks_written: int


def chunk_text(text: str, *, chunk_size: int = 1000, overlap: int = 200) -> list[str]:
    """Split ``text`` into overlapping fixed-size character chunks.

    Character-based (not token-based) chunking — simple and dependency-free.
    Returns an empty list for blank text. Raises ``ValueError`` for invalid sizes.
    """
    if chunk_size < 1:
        raise ValueError("chunk_size must be >= 1")
    if not 0 <= overlap < chunk_size:
        raise ValueError("overlap must be in the range [0, chunk_size)")
    stripped = text.strip()
    if not stripped:
        return []
    step = chunk_size - overlap
    return [stripped[start : start + chunk_size] for start in range(0, len(stripped), step)]


async def ingest_documents(
    connector: Any,
    documents: Sequence[SourceDocument],
    scope: MemoryScope,
    *,
    chunk_size: int = 1000,
    overlap: int = 200,
) -> IngestionReport:
    """Chunk each document and write its chunks to ``connector`` (embedding delegated).

    Each chunk is written under key ``{source_id}#{index}`` with the chunk text as
    the value, so a vector connector embeds the text and a RetrievalNode can later
    attribute the chunk back to ``source_id``. (The connector ``write`` interface
    carries no metadata field, so source attribution travels via the key.)
    """
    chunks_written = 0
    for document in documents:
        chunks = chunk_text(document.text, chunk_size=chunk_size, overlap=overlap)
        for index, chunk in enumerate(chunks):
            await connector.write(f"{document.source_id}#{index}", chunk, scope)
            chunks_written += 1
    return IngestionReport(documents=len(documents), chunks_written=chunks_written)
