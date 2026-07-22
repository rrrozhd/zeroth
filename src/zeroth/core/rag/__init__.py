"""Legacy import path for the rag integrations package.

RAG ingestion lives in :mod:`zeroth.integrations.rag`; this package
republishes the same objects for compatibility. Import from the canonical
location instead (see docs/backend-import-migration.md).
"""

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
