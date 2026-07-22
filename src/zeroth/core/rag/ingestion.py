"""Legacy import path for :mod:`zeroth.integrations.rag.ingestion`."""

from zeroth.integrations.rag.ingestion import (
    IngestionReport,
    SourceDocument,
    chunk_text,
    ingest_documents,
)

__all__ = [
    "IngestionReport",
    "SourceDocument",
    "chunk_text",
    "ingest_documents",
]
