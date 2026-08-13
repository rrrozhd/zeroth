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

from zeroth.contracts.governed.models.memory import MemoryScope


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


@dataclass(frozen=True)
class IngestionCursor:
    """Where an interrupted ingest stopped, so the caller can resume it."""

    document_index: int
    """Index into the ``documents`` sequence that was being ingested."""

    source_id: str
    """``source_id`` of that document, for logs and reconciliation."""

    chunk_index: int
    """Index of the chunk whose write failed. Chunks before it are indexed."""


class PartialIngestionError(RuntimeError):
    """An ingest failed with chunks already durably written (RAG-02 / A07-23).

    The sink is a vector/KV backend, so there is no transaction to join and no
    way to unwind what already landed. What the caller needs instead is to
    *know*: ``report`` is what is durably indexed, and ``cursor`` names the
    document and chunk that failed. Because writes are keyed
    ``{source_id}#{index}`` and therefore idempotent, resuming is re-offering
    ``documents[cursor.document_index:]`` -- the partially-written document is
    rewritten whole over the same keys.
    """

    def __init__(self, report: IngestionReport, cursor: IngestionCursor) -> None:
        super().__init__(
            f"ingest failed on document {cursor.document_index}"
            f" ({cursor.source_id!r}) at chunk {cursor.chunk_index};"
            f" {report.chunks_written} chunk(s) across {report.documents}"
            " whole document(s) are already indexed"
        )
        self.report = report
        self.cursor = cursor


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

    A07-23: writes land one chunk at a time in a backend with no transaction, so
    a failure partway leaves earlier chunks durably indexed. Rather than let the
    connector's error escape with the report unbuilt -- chunks in the index and
    no record of which -- a failed write raises
    :class:`PartialIngestionError`, carrying what landed and where to resume.
    The original error is the ``__cause__``.
    """
    chunks_written = 0
    for document_index, document in enumerate(documents):
        chunks = chunk_text(document.text, chunk_size=chunk_size, overlap=overlap)
        for index, chunk in enumerate(chunks):
            try:
                await connector.write(f"{document.source_id}#{index}", chunk, scope)
            except Exception as exc:
                raise PartialIngestionError(
                    # Everything before ``document_index`` was written whole:
                    # the raise happens inside the loop body, before the index
                    # advances.
                    IngestionReport(
                        documents=document_index,
                        chunks_written=chunks_written,
                    ),
                    IngestionCursor(
                        document_index=document_index,
                        source_id=document.source_id,
                        chunk_index=index,
                    ),
                ) from exc
            chunks_written += 1
    return IngestionReport(documents=len(documents), chunks_written=chunks_written)
