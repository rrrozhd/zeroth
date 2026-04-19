"""Artifact store protocol and backend implementations.

Defines the ArtifactStore protocol that all storage backends must satisfy,
plus Redis and filesystem implementations. The protocol specifies six async
methods for storing, retrieving, deleting, refreshing TTL, checking existence,
and cleaning up run artifacts.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from zeroth.core.artifacts.errors import (
    ArtifactNotFoundError,
    ArtifactStorageError,
    ArtifactTTLError,
)
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

    Args:
        redis_url: Redis connection URL.
        prefix: Key prefix for namespace isolation.
        default_ttl: Default TTL in seconds when none is specified.
        max_size: Maximum artifact payload size in bytes.
    """

    def __init__(
        self,
        redis_url: str,
        prefix: str = "zeroth:artifact",
        default_ttl: int = 3600,
        max_size: int = 104857600,
        *,
        client: Any | None = None,
    ) -> None:
        if client is not None:
            self._client = client
        else:
            import redis.asyncio as aioredis

            self._client = aioredis.from_url(redis_url)
        self._prefix = prefix
        self._default_ttl = default_ttl
        self._max_size = max_size

    def _full_key(self, key: str) -> str:
        """Build the full Redis key with prefix."""
        return f"{self._prefix}:{key}"

    def _meta_key(self, key: str) -> str:
        """Build the metadata Redis key with prefix."""
        return f"{self._prefix}:{key}:meta"

    async def store(
        self,
        key: str,
        data: bytes,
        content_type: str,
        ttl: int | None = None,
    ) -> ArtifactReference:
        """Store artifact data and metadata atomically via pipeline.

        Uses SETEX when TTL is provided, plain SET otherwise. Both the data
        key and metadata key are written in a single pipeline transaction.

        Args:
            key: Artifact key in {run_id}/{node_id}/{uuid} format.
            data: Binary artifact payload.
            content_type: MIME type of the artifact.
            ttl: Time-to-live in seconds. None means no expiration.

        Returns:
            ArtifactReference pointing to the stored artifact.

        Raises:
            ArtifactStorageError: If payload exceeds max size.
        """
        if len(data) > self._max_size:
            msg = f"Artifact size {len(data)} exceeds maximum {self._max_size} bytes"
            raise ArtifactStorageError(msg)

        full_key = self._full_key(key)
        meta_key = self._meta_key(key)

        now = datetime.now(UTC)
        meta: dict[str, Any] = {
            "content_type": content_type,
            "size": len(data),
            "created_at": now.isoformat(),
            "ttl_seconds": ttl,
        }
        meta_bytes = json.dumps(meta).encode()

        async with self._client.pipeline(transaction=True) as pipe:
            if ttl is not None:
                pipe.setex(full_key, ttl, data)
                pipe.setex(meta_key, ttl, meta_bytes)
            else:
                pipe.set(full_key, data)
                pipe.set(meta_key, meta_bytes)
            await pipe.execute()

        return ArtifactReference(
            store="redis",
            key=key,
            content_type=content_type,
            size=len(data),
            created_at=now,
            ttl_seconds=ttl,
        )

    async def retrieve(self, key: str) -> bytes:
        """Retrieve artifact data by key.

        Args:
            key: Artifact key to retrieve.

        Returns:
            Raw bytes of the stored artifact.

        Raises:
            ArtifactNotFoundError: If the key does not exist in Redis.
        """
        full_key = self._full_key(key)
        data = await self._client.get(full_key)
        if data is None:
            msg = f"Artifact not found: {key}"
            raise ArtifactNotFoundError(msg)
        return data

    async def delete(self, key: str) -> bool:
        """Delete an artifact and its metadata.

        Args:
            key: Artifact key to delete.

        Returns:
            True if the artifact existed and was deleted, False otherwise.
        """
        full_key = self._full_key(key)
        meta_key = self._meta_key(key)

        async with self._client.pipeline(transaction=True) as pipe:
            pipe.delete(full_key)
            pipe.delete(meta_key)
            results = await pipe.execute()

        return sum(results) > 0

    async def refresh_ttl(self, key: str, ttl: int) -> bool:
        """Refresh the TTL of an existing artifact.

        Args:
            key: Artifact key to refresh.
            ttl: New TTL in seconds.

        Returns:
            True if the TTL was refreshed successfully.

        Raises:
            ArtifactTTLError: If the artifact does not exist.
        """
        full_key = self._full_key(key)
        if not await self._client.exists(full_key):
            msg = f"Cannot refresh TTL for missing artifact: {key}"
            raise ArtifactTTLError(msg)

        meta_key = self._meta_key(key)
        async with self._client.pipeline(transaction=True) as pipe:
            pipe.expire(full_key, ttl)
            pipe.expire(meta_key, ttl)
            await pipe.execute()

        return True

    async def exists(self, key: str) -> bool:
        """Check whether an artifact exists.

        Args:
            key: Artifact key to check.

        Returns:
            True if the artifact exists in Redis.
        """
        full_key = self._full_key(key)
        return bool(await self._client.exists(full_key))

    async def cleanup_run(self, run_id: str) -> int:
        """Remove all artifacts for a run using scan_iter.

        Scans for all keys matching the run_id prefix and deletes them.

        Args:
            run_id: Run identifier whose artifacts should be cleaned up.

        Returns:
            Count of deleted keys.
        """
        pattern = f"{self._prefix}:{run_id}/*"
        count = 0
        async for redis_key in self._client.scan_iter(match=pattern, count=100):
            await self._client.delete(redis_key)
            count += 1
        return count


class FilesystemArtifactStore:
    """Filesystem-backed artifact store with .meta.json sidecars.

    Stores artifact data as files with companion metadata sidecars.
    All blocking I/O is wrapped in asyncio.to_thread for async compatibility.
    Implements lazy TTL expiration on retrieve.

    Args:
        base_dir: Base directory for artifact file storage.
        default_ttl: Default TTL in seconds when none is specified.
        max_size: Maximum artifact payload size in bytes.
    """

    def __init__(
        self,
        base_dir: str | Path,
        default_ttl: int = 3600,
        max_size: int = 104857600,
    ) -> None:
        self._base_dir = Path(base_dir)
        self._default_ttl = default_ttl
        self._max_size = max_size

    def _validate_key(self, key: str) -> None:
        """Reject keys containing path traversal sequences.

        Args:
            key: Artifact key to validate.

        Raises:
            ArtifactStorageError: If the key contains '..' segments.
        """
        if ".." in key.split("/"):
            msg = f"Rejected key with path traversal: {key}"
            raise ArtifactStorageError(msg)

    def _file_path(self, key: str) -> Path:
        """Resolve the filesystem path for an artifact key."""
        return self._base_dir / key

    def _meta_path(self, key: str) -> Path:
        """Resolve the filesystem path for an artifact's sidecar metadata."""
        return self._base_dir / f"{key}.meta.json"

    def _write_file(self, key: str, data: bytes, meta: dict[str, Any]) -> None:
        """Synchronous file write for use with asyncio.to_thread."""
        file_path = self._file_path(key)
        meta_path = self._meta_path(key)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(data)
        meta_path.write_text(json.dumps(meta))

    def _read_file(self, key: str) -> bytes:
        """Synchronous file read for use with asyncio.to_thread."""
        file_path = self._file_path(key)
        if not file_path.exists():
            msg = f"Artifact not found: {key}"
            raise ArtifactNotFoundError(msg)
        return file_path.read_bytes()

    def _read_meta(self, key: str) -> dict[str, Any]:
        """Synchronous metadata read for use with asyncio.to_thread."""
        meta_path = self._meta_path(key)
        if not meta_path.exists():
            msg = f"Artifact metadata not found: {key}"
            raise ArtifactNotFoundError(msg)
        return json.loads(meta_path.read_text())

    def _is_expired(self, meta: dict[str, Any]) -> bool:
        """Check if an artifact has expired based on its sidecar metadata."""
        expires_at_str = meta.get("expires_at")
        if expires_at_str is None:
            return False
        expires_at = datetime.fromisoformat(expires_at_str)
        return datetime.now(UTC) >= expires_at

    def _delete_files(self, key: str) -> bool:
        """Synchronous file deletion for use with asyncio.to_thread."""
        file_path = self._file_path(key)
        meta_path = self._meta_path(key)
        existed = file_path.exists()
        if file_path.exists():
            file_path.unlink()
        if meta_path.exists():
            meta_path.unlink()
        return existed

    async def store(
        self,
        key: str,
        data: bytes,
        content_type: str,
        ttl: int | None = None,
    ) -> ArtifactReference:
        """Store artifact data as a file with a .meta.json sidecar.

        Creates parent directories as needed. Validates key against path
        traversal and payload against size limits.

        Args:
            key: Artifact key in {run_id}/{node_id}/{uuid} format.
            data: Binary artifact payload.
            content_type: MIME type of the artifact.
            ttl: Time-to-live in seconds. None means no expiration.

        Returns:
            ArtifactReference pointing to the stored artifact.

        Raises:
            ArtifactStorageError: If key contains path traversal or payload exceeds max size.
        """
        self._validate_key(key)

        if len(data) > self._max_size:
            msg = f"Artifact size {len(data)} exceeds maximum {self._max_size} bytes"
            raise ArtifactStorageError(msg)

        now = datetime.now(UTC)
        expires_at = (now + timedelta(seconds=ttl)).isoformat() if ttl is not None else None

        meta: dict[str, Any] = {
            "content_type": content_type,
            "size": len(data),
            "created_at": now.isoformat(),
            "ttl_seconds": ttl,
            "expires_at": expires_at,
        }

        await asyncio.to_thread(self._write_file, key, data, meta)

        return ArtifactReference(
            store="filesystem",
            key=key,
            content_type=content_type,
            size=len(data),
            created_at=now,
            ttl_seconds=ttl,
        )

    async def retrieve(self, key: str) -> bytes:
        """Retrieve artifact data from the filesystem.

        Checks the sidecar for TTL expiration. If expired, performs lazy
        cleanup (deletes both files) and raises ArtifactNotFoundError.

        Args:
            key: Artifact key to retrieve.

        Returns:
            Raw bytes of the stored artifact.

        Raises:
            ArtifactNotFoundError: If the artifact is missing or expired.
        """
        self._validate_key(key)

        try:
            meta = await asyncio.to_thread(self._read_meta, key)
        except ArtifactNotFoundError:
            msg = f"Artifact not found: {key}"
            raise ArtifactNotFoundError(msg) from None

        if self._is_expired(meta):
            # Lazy cleanup of expired artifact
            await asyncio.to_thread(self._delete_files, key)
            msg = f"Artifact expired: {key}"
            raise ArtifactNotFoundError(msg)

        return await asyncio.to_thread(self._read_file, key)

    async def delete(self, key: str) -> bool:
        """Delete an artifact and its sidecar.

        Args:
            key: Artifact key to delete.

        Returns:
            True if the artifact existed and was deleted, False otherwise.
        """
        self._validate_key(key)
        return await asyncio.to_thread(self._delete_files, key)

    async def refresh_ttl(self, key: str, ttl: int) -> bool:
        """Refresh the TTL of an existing artifact.

        Updates the expires_at and ttl_seconds fields in the sidecar metadata.

        Args:
            key: Artifact key to refresh.
            ttl: New TTL in seconds.

        Returns:
            True if the TTL was refreshed successfully.

        Raises:
            ArtifactTTLError: If the artifact does not exist.
        """
        self._validate_key(key)
        meta_path = self._meta_path(key)

        def _refresh() -> bool:
            if not meta_path.exists():
                msg = f"Cannot refresh TTL for missing artifact: {key}"
                raise ArtifactTTLError(msg)
            meta = json.loads(meta_path.read_text())
            now = datetime.now(UTC)
            meta["ttl_seconds"] = ttl
            meta["expires_at"] = (now + timedelta(seconds=ttl)).isoformat()
            meta_path.write_text(json.dumps(meta))
            return True

        return await asyncio.to_thread(_refresh)

    async def exists(self, key: str) -> bool:
        """Check whether an artifact exists and is not expired.

        Args:
            key: Artifact key to check.

        Returns:
            True if the artifact exists and has not expired.
        """
        self._validate_key(key)

        def _check() -> bool:
            file_path = self._file_path(key)
            if not file_path.exists():
                return False
            meta_path = self._meta_path(key)
            if not meta_path.exists():
                return False
            meta = json.loads(meta_path.read_text())
            return not self._is_expired(meta)

        return await asyncio.to_thread(_check)

    async def cleanup_run(self, run_id: str) -> int:
        """Remove all artifacts for a run by deleting the run directory tree.

        Args:
            run_id: Run identifier whose artifacts should be cleaned up.

        Returns:
            Count of deleted artifact files (excluding sidecars).
        """

        def _cleanup() -> int:
            run_dir = self._base_dir / run_id
            if not run_dir.exists():
                return 0
            # Count actual artifact files (not sidecars)
            count = sum(
                1 for f in run_dir.rglob("*") if f.is_file() and not f.name.endswith(".meta.json")
            )
            shutil.rmtree(run_dir)
            return count

        return await asyncio.to_thread(_cleanup)
