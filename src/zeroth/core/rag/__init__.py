"""Retrieval-augmented generation (RAG) helpers.

The RetrievalNode itself lives in ``zeroth.contracts.graph`` (it is a graph node type);
this package provides the ingestion side — chunking documents and writing them to a
memory connector for later retrieval.
"""

from zeroth.core.rag.ingestion import (
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
