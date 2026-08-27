"""ChromaDBMemoryConnector: vector similarity search via ChromaDB HTTP client.

Implements governed MemoryConnector protocol using an external ChromaDB
server for vector storage and similarity search. Embeddings are generated
internally via litellm.

Per D-10, D-12, D-14 from Phase 14 planning.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

import chromadb
import litellm

from zeroth.contracts.governed.models.common import JSONValue
from zeroth.integrations.memory.embedding_calls import (
    invoke_embedding_call,
    resolve_embedding_provider_kwargs,
)
from zeroth.integrations.memory.governed.models import MemoryEntry, MemoryScope

# ChromaDB collection names must be 3-512 chars from [a-zA-Z0-9._-] and must
# start and end with an alphanumeric character. Collapse any run of other
# characters to a single underscore so arbitrary scope targets stay valid.
_COLLECTION_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9]+")
_LOCAL_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)

# Explicitly opt-in, provider-free embedding model for deterministic local
# fixtures and air-gapped development. This is a lexical hashing model, not a
# substitute for a semantic production embedding model.
LOCAL_HASH_EMBEDDING_MODEL = "zeroth/local-hash-bow-v1"
_LOCAL_HASH_DIMENSIONS = 256


def local_hash_embedding(text: str) -> list[float]:
    """Return a deterministic, normalized lexical vector without external I/O.

    SHA-256 fixes the mapping across processes and Python versions (unlike
    ``hash()``). Signed feature hashing keeps the vector bounded while token
    counts preserve simple lexical similarity. No input text or digest is
    persisted by this function.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("local hash embedding requires non-empty text")
    tokens = _LOCAL_TOKEN_RE.findall(text.casefold())
    if not tokens:
        raise ValueError("local hash embedding requires at least one word token")
    vector = [0.0] * _LOCAL_HASH_DIMENSIONS
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % _LOCAL_HASH_DIMENSIONS
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(component * component for component in vector))
    if norm == 0:
        raise ValueError("local hash embedding produced a zero vector")
    return [component / norm for component in vector]


class MemoryBackendResponseError(RuntimeError):
    """A backend answered with a payload this connector cannot read.

    Raised in place of the raw ``KeyError``/``IndexError`` that indexing into a
    malformed response produces. Both of those misreport the condition. A
    ``KeyError`` out of a memory connector is this layer's own *not-found*
    signal -- ``delete`` raises it deliberately, and
    ``zeroth.platform.primitives.error_vocabulary`` maps ``key_error`` to
    ``ErrorCategory.NOT_FOUND`` -- so an unparseable response would surface as
    "no such entry". An ``IndexError`` reads as a bug in this module. The real
    condition is a peer answering with something other than what its API
    documents, which is a backend fault.

    Deliberately **not** a subclass of ``KeyError`` or ``IndexError``: callers
    already branch on those, and inheriting would reinstate exactly the
    confusion this type exists to remove.
    """


def _response_field(payload: Any, field: str, operation: str) -> Any:
    """Read one documented field out of a ChromaDB response.

    Args:
        payload: The object ChromaDB returned.
        field: Field the ChromaDB API documents for this call.
        operation: ChromaDB method name, used in the error message.

    Returns:
        The field's value.

    Raises:
        MemoryBackendResponseError: The payload is not a mapping, or the field
            is absent.

    """
    if not isinstance(payload, Mapping) or field not in payload:
        raise MemoryBackendResponseError(
            f"chroma {operation}() response is missing the {field!r} field"
        )
    return payload[field]


def _element(value: Any, index: int, field: str, operation: str) -> Any:
    """Index into a ChromaDB response field without leaking ``IndexError``.

    Args:
        value: The field's value, as returned by ChromaDB.
        index: Position within that field.
        field: Field name, used in the error message.
        operation: ChromaDB method name, used in the error message.

    Returns:
        The element at ``index``.

    Raises:
        MemoryBackendResponseError: The value is not indexable, or is shorter
            than ``index`` requires.

    """
    try:
        return value[index]
    except (IndexError, KeyError, TypeError) as exc:
        raise MemoryBackendResponseError(
            f"chroma {operation}() response field {field!r} has no element {index}"
        ) from exc


def _response_row(payload: Any, field: str, index: int, operation: str) -> Any:
    """Read one positional element out of a ChromaDB response field.

    Args:
        payload: The object ChromaDB returned.
        field: Field the ChromaDB API documents for this call.
        index: Position within that field.
        operation: ChromaDB method name, used in the error message.

    Returns:
        The element at ``index``.

    Raises:
        MemoryBackendResponseError: The field is absent, is not indexable, or
            is shorter than ``index`` requires.

    """
    return _element(_response_field(payload, field, operation), index, field, operation)


def _decode_document(document: Any, operation: str) -> Any:
    """Decode a stored document, or return ``None`` when ChromaDB held nothing.

    Args:
        document: The raw document string ChromaDB returned.
        operation: ChromaDB method name, used in the error message.

    Returns:
        The decoded JSON value, or ``None`` for an empty document.

    Raises:
        MemoryBackendResponseError: The document is not decodable JSON.

    """
    if not document:
        return None
    try:
        return json.loads(document)
    except (TypeError, ValueError) as exc:
        raise MemoryBackendResponseError(
            f"chroma {operation}() returned a document that is not valid JSON"
        ) from exc


def _embedding_vector(response: Any) -> list[float]:
    """Extract the embedding vector out of a litellm embedding response.

    A truncated or reshaped provider response otherwise fails deep inside
    ChromaDB -- or, worse, is accepted and stored -- rather than at the
    boundary that received it.

    Args:
        response: Whatever ``litellm.aembedding`` returned.

    Returns:
        The embedding as a list of floats.

    Raises:
        MemoryBackendResponseError: The response carries no usable vector.

    """
    data = getattr(response, "data", None)
    if not isinstance(data, Sequence) or isinstance(data, str | bytes) or not data:
        raise MemoryBackendResponseError("embedding response carries no 'data' entries")
    first = data[0]
    if not isinstance(first, Mapping) or "embedding" not in first:
        raise MemoryBackendResponseError("embedding response entry has no 'embedding' field")
    vector = first["embedding"]
    if not isinstance(vector, Sequence) or isinstance(vector, str | bytes) or not vector:
        raise MemoryBackendResponseError("embedding response carries an empty embedding vector")
    if any(isinstance(v, bool) or not isinstance(v, int | float) for v in vector):
        raise MemoryBackendResponseError("embedding vector contains a non-numeric component")
    return [float(v) for v in vector]


class ChromaDBMemoryConnector:
    """Memory connector backed by an external ChromaDB server.

    Uses ChromaDB's HTTP client to connect to a running ChromaDB instance.
    Each scope+target combination maps to a separate collection with
    cosine similarity configured.
    """

    connector_type = "chroma"

    def __init__(
        self,
        client: chromadb.HttpClient,
        *,
        collection_prefix: str = "zeroth_memory",
        embedding_model: str | None = None,
    ) -> None:
        # Resolve the embedding default lazily to avoid a module-load import of
        # config.settings, which forms a circular import (settings ↔ connector) that can
        # silently disable this backend depending on import order.
        if embedding_model is None:
            from zeroth.platform.config.settings import DEFAULT_EMBEDDING_MODEL

            embedding_model = DEFAULT_EMBEDDING_MODEL
        self._client = client
        self._collection_prefix = collection_prefix
        self._embedding_model = embedding_model
        self._embedding_secret_provider = None
        self._embedding_tenant_id: str | None = None
        self._embedding_allow_env_fallback = True

    def configure_embedding_secrets(
        self,
        *,
        secret_provider: Any | None,
        tenant_id: str | None,
        allow_env_fallback: bool,
    ) -> None:
        """Bind tenant-scoped credential resolution without changing the pinned constructor."""
        self._embedding_secret_provider = secret_provider
        self._embedding_tenant_id = tenant_id
        self._embedding_allow_env_fallback = allow_env_fallback

    def _collection_name(self, scope: MemoryScope, target: str | None) -> str:
        """Build a valid ChromaDB collection name from scope and target.

        Collapses any run of non-alphanumeric characters in the target to a
        single underscore and trims the ends, so targets that contain or are
        padded with separators -- notably the canonical SHARED target
        ``__shared__`` -- still produce a name ChromaDB accepts (one that
        starts and ends with an alphanumeric character).
        """
        safe_target = _COLLECTION_SANITIZE_RE.sub("_", target or "default").strip("_") or "default"
        return f"{self._collection_prefix}_{scope.value}_{safe_target}"

    async def _get_collection(self, scope: MemoryScope, target: str | None) -> Any:
        """Get or create a ChromaDB collection for this scope+target.

        ``get_or_create_collection`` is a blocking HTTP round-trip, and it runs
        ahead of every read/write/delete/search -- so leaving it on the event
        loop stalled the whole process twice per operation, not once. Every
        chromadb call in this module goes through ``asyncio.to_thread`` for the
        same reason: the client is synchronous, and this class is not.
        """
        return await asyncio.to_thread(
            self._client.get_or_create_collection,
            name=self._collection_name(scope, target),
            metadata={"hnsw:space": "cosine"},
        )

    async def _embed(self, text: str) -> list[float]:
        """Generate embedding vector via litellm."""
        if self._embedding_model == LOCAL_HASH_EMBEDDING_MODEL:
            return local_hash_embedding(text)
        provider_kwargs = await resolve_embedding_provider_kwargs(
            model=self._embedding_model,
            secret_provider=self._embedding_secret_provider,
            tenant_id=self._embedding_tenant_id,
            allow_env_fallback=self._embedding_allow_env_fallback,
        )
        response = await invoke_embedding_call(
            model=self._embedding_model,
            inputs=[text],
            provider_call=lambda: litellm.aembedding(
                model=self._embedding_model,
                input=[text],
                **provider_kwargs,
            ),
        )
        return _embedding_vector(response)

    async def read(
        self, key: str, scope: MemoryScope, *, target: str | None = None
    ) -> MemoryEntry | None:
        """Look up a memory entry by key from the appropriate collection."""
        collection = await self._get_collection(scope, target)
        result = await asyncio.to_thread(
            collection.get, ids=[key], include=["documents", "metadatas"]
        )
        if not _response_field(result, "ids", "get"):
            return None
        return MemoryEntry(
            key=key,
            value=_decode_document(_response_row(result, "documents", 0, "get"), "get"),
            scope=scope,
            scope_target=target or "",
            metadata=_response_row(result, "metadatas", 0, "get") or {},
        )

    async def write(
        self, key: str, value: JSONValue, scope: MemoryScope, *, target: str | None = None
    ) -> None:
        """Store a value with its embedding in ChromaDB. Uses upsert for idempotent writes."""
        collection = await self._get_collection(scope, target)
        text = (
            f"{key}: {json.dumps(value)}" if isinstance(value, dict | list) else f"{key}: {value}"
        )
        embedding = await self._embed(text)
        await asyncio.to_thread(
            collection.upsert,
            ids=[key],
            documents=[json.dumps(value)],
            embeddings=[embedding],
            metadatas=[{"key": key, "scope": scope.value, "target": target or ""}],
        )

    async def delete(self, key: str, scope: MemoryScope, *, target: str | None = None) -> None:
        """Remove a memory entry. Raises KeyError if not found."""
        collection = await self._get_collection(scope, target)
        existing = await asyncio.to_thread(collection.get, ids=[key])
        if not _response_field(existing, "ids", "get"):
            raise KeyError(key)
        await asyncio.to_thread(collection.delete, ids=[key])

    async def search(
        self, query: dict, scope: MemoryScope, *, target: str | None = None
    ) -> list[MemoryEntry]:
        """Semantic search using cosine similarity via ChromaDB."""
        collection = await self._get_collection(scope, target)
        text = query.get("text", "")
        limit = query.get("limit", 10)
        embedding = await self._embed(text)
        results = await asyncio.to_thread(
            collection.query,
            query_embeddings=[embedding],
            n_results=limit,
            include=["documents", "metadatas"],
        )
        # query() answers per-query-embedding, so every field is a list of one
        # list here. A flat response is a malformed one, not an empty result.
        ids = _response_row(results, "ids", 0, "query")
        documents = _response_row(results, "documents", 0, "query")
        raw_metadatas = _response_field(results, "metadatas", "query")
        metadatas = _element(raw_metadatas, 0, "metadatas", "query") if raw_metadatas else None
        entries = []
        for i, doc_id in enumerate(ids):
            meta = _element(metadatas, i, "metadatas", "query") if metadatas else {}
            entries.append(
                MemoryEntry(
                    key=doc_id,
                    value=_decode_document(_element(documents, i, "documents", "query"), "query"),
                    scope=scope,
                    scope_target=target or "",
                    metadata=meta or {},
                )
            )
        return entries
