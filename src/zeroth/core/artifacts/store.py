"""Artifact store protocol and backend implementations.

Defines the ArtifactStore protocol that all storage backends must satisfy,
plus Redis and filesystem implementations. The protocol specifies six async
methods for storing, retrieving, deleting, refreshing TTL, checking existence,
and cleaning up run artifacts.
"""

from __future__ import annotations

from typing import Protocol

from zeroth.core.artifacts.models import ArtifactReference


class ArtifactStore(Protocol):
    """Protocol defining the artifact storage interface.

    Any class implementing these six async methods can serve as an artifact
    storage backend. The protocol uses structural subtyping so implementations
    do not need to explicitly inherit from this class.
    """

    async def store(
        self,
        key: str,
        data: bytes,
        content_type: str,
        ttl: int | None = None,
    ) -> ArtifactReference:  # pragma: no cover - protocol
        """Store artifact data and return a reference pointer."""
        ...

    async def retrieve(self, key: str) -> bytes:  # pragma: no cover - protocol
        """Retrieve artifact data by key."""
        ...

    async def delete(self, key: str) -> bool:  # pragma: no cover - protocol
        """Delete an artifact by key. Returns True if it existed."""
        ...

    async def refresh_ttl(self, key: str, ttl: int) -> bool:  # pragma: no cover - protocol
        """Refresh the TTL of an existing artifact. Returns True on success."""
        ...

    async def exists(self, key: str) -> bool:  # pragma: no cover - protocol
        """Check whether an artifact exists."""
        ...

    async def cleanup_run(self, run_id: str) -> int:  # pragma: no cover - protocol
        """Remove all artifacts for a run. Returns count of deleted artifacts."""
        ...


class RedisArtifactStore:
    """Redis-backed artifact store using pipeline-atomic SETEX operations.

    Stores artifact data and metadata as separate Redis keys with optional
    TTL. Uses scan_iter for prefix-based bulk cleanup of run artifacts.
    """


class FilesystemArtifactStore:
    """Filesystem-backed artifact store with .meta.json sidecars.

    Stores artifact data as files with companion metadata sidecars.
    All blocking I/O is wrapped in asyncio.to_thread for async compatibility.
    Implements lazy TTL expiration on retrieve.
    """
