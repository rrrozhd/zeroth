"""Artifact store protocol and backend implementations.

Defines the ArtifactStore protocol that all storage backends must satisfy,
plus Redis and filesystem implementations. The protocol specifies six async
methods for storing, retrieving, deleting, refreshing TTL, checking existence,
and cleaning up run artifacts.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from secrets import token_hex
from typing import Any, Protocol

from zeroth.platform.artifacts.errors import (
    ArtifactNotFoundError,
    ArtifactStorageError,
    ArtifactTTLError,
)
from zeroth.platform.artifacts.models import ArtifactReference

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised only on unsupported platforms
    fcntl = None  # type: ignore[assignment]


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

    async def delete(
        self, key: str, *, idempotency_key: str
    ) -> bool:  # pragma: no cover - protocol
        """Delete an artifact by key. Returns True if it existed."""
        ...

    async def refresh_ttl(self, key: str, ttl: int) -> bool:  # pragma: no cover - protocol
        """Refresh the TTL of an existing artifact. Returns True on success."""
        ...

    async def exists(self, key: str) -> bool:  # pragma: no cover - protocol
        """Check whether an artifact exists."""
        ...

    async def cleanup_run(
        self, run_id: str, *, idempotency_key: str
    ) -> int:  # pragma: no cover - protocol
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
        return f"{self._prefix}:data:{key}"

    def _meta_key(self, key: str) -> str:
        """Build the metadata Redis key with prefix."""
        return f"{self._prefix}:meta:{key}"

    def _legacy_full_key(self, key: str) -> str:
        return f"{self._prefix}:{key}"

    def _legacy_meta_key(self, key: str) -> str:
        return f"{self._prefix}:{key}:meta"

    def _receipt_key(self, idempotency_key: str) -> str:
        """Build the Redis key for an erasure-operation replay receipt."""
        return f"{self._prefix}:erasure-receipt:v2:{idempotency_key}"

    def _legacy_receipt_key(self, idempotency_key: str) -> str:
        return f"{self._prefix}:erasure-receipt:{idempotency_key}"

    @staticmethod
    def _receipt_payload(*, kind: str, target: str, result: bool | int) -> str:
        return json.dumps(
            {"version": 2, "kind": kind, "target": target, "result": result},
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _receipt_result(
        raw_receipt: bytes | str,
        *,
        kind: str,
        target: str,
        result_type: type[bool] | type[int],
        allow_prior_version: bool = False,
    ) -> bool | int:
        try:
            receipt = json.loads(raw_receipt)
        except (TypeError, ValueError):
            raise ArtifactStorageError("Invalid erasure operation receipt") from None
        valid_version = isinstance(receipt, dict) and (
            receipt.get("version") == 2 or (allow_prior_version and "version" not in receipt)
        )
        if not valid_version or receipt.get("kind") != kind or receipt.get("target") != target:
            raise ArtifactStorageError("idempotency key reused for another operation")
        result = receipt.get("result")
        if result_type is bool:
            if type(result) is not bool:
                raise ArtifactStorageError("Invalid erasure operation receipt")
        elif type(result) is not int or result < 0:
            raise ArtifactStorageError("Invalid erasure operation receipt")
        return result

    async def _existing_receipt(
        self,
        idempotency_key: str,
        *,
        kind: str,
        target: str,
        result_type: type[bool] | type[int],
    ) -> tuple[bool, bool | int]:
        """Return a validated replay result, migrating the prior JSON namespace."""
        receipt_key = self._receipt_key(idempotency_key)
        receipt = await self._client.get(receipt_key)
        if receipt is not None:
            return True, self._receipt_result(
                receipt,
                kind=kind,
                target=target,
                result_type=result_type,
            )

        prior_receipt = await self._client.get(self._legacy_receipt_key(idempotency_key))
        if prior_receipt is None:
            return False, False if result_type is bool else 0
        try:
            prior_value = json.loads(prior_receipt)
        except (TypeError, ValueError):
            raise ArtifactStorageError("Legacy erasure receipt blocks unbound replay") from None
        if not isinstance(prior_value, dict):
            raise ArtifactStorageError("Legacy erasure receipt blocks unbound replay")

        prior_result = self._receipt_result(
            prior_receipt,
            kind=kind,
            target=target,
            result_type=result_type,
            allow_prior_version=True,
        )
        await self._client.set(
            receipt_key,
            self._receipt_payload(kind=kind, target=target, result=prior_result),
            nx=True,
        )
        migrated_receipt = await self._client.get(receipt_key)
        if migrated_receipt is None:
            raise ArtifactStorageError("Invalid erasure operation receipt")
        return True, self._receipt_result(
            migrated_receipt,
            kind=kind,
            target=target,
            result_type=result_type,
        )

    async def _claim_receipt(
        self,
        idempotency_key: str,
        *,
        kind: str,
        target: str,
        result: bool | int,
        result_type: type[bool] | type[int],
    ) -> tuple[bool, bool | int]:
        """Atomically bind an operation and report whether this caller won."""
        receipt_key = self._receipt_key(idempotency_key)
        claimed = bool(
            await self._client.set(
                receipt_key,
                self._receipt_payload(kind=kind, target=target, result=result),
                nx=True,
            )
        )
        receipt = await self._client.get(receipt_key)
        if receipt is None:
            if claimed:
                return True, result
            raise ArtifactStorageError("Invalid erasure operation receipt")
        stable_result = self._receipt_result(
            receipt,
            kind=kind,
            target=target,
            result_type=result_type,
        )
        return claimed, stable_result

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
            key: Logical artifact key in a cleanup-compatible grammar.
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
            data = await self._client.get(self._legacy_full_key(key))
        if data is None:
            msg = f"Artifact not found: {key}"
            raise ArtifactNotFoundError(msg)
        return data

    async def delete(self, key: str, *, idempotency_key: str) -> bool:
        """Delete an artifact and its metadata.

        Args:
            key: Artifact key to delete.
            idempotency_key: Stable operation identifier used for replay receipts.

        Returns:
            True if the artifact existed and was deleted, False otherwise.
        """
        full_key = self._full_key(key)
        meta_key = self._meta_key(key)
        legacy_full_key = self._legacy_full_key(key)
        legacy_meta_key = self._legacy_meta_key(key)
        has_receipt, result = await self._existing_receipt(
            idempotency_key,
            kind="delete",
            target=key,
            result_type=bool,
        )
        if has_receipt:
            return bool(result)

        existed = bool(
            await self._client.exists(full_key) or await self._client.exists(legacy_full_key)
        )
        claimed, stable_result = await self._claim_receipt(
            idempotency_key,
            kind="delete",
            target=key,
            result=existed,
            result_type=bool,
        )
        if not claimed:
            return bool(stable_result)

        async with self._client.pipeline(transaction=True) as pipe:
            pipe.delete(full_key)
            pipe.delete(meta_key)
            pipe.delete(legacy_full_key)
            pipe.delete(legacy_meta_key)
            await pipe.execute()

        return stable_result

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
        meta_key = self._meta_key(key)
        if not await self._client.exists(full_key):
            full_key = self._legacy_full_key(key)
            meta_key = self._legacy_meta_key(key)
        if not await self._client.exists(full_key):
            msg = f"Cannot refresh TTL for missing artifact: {key}"
            raise ArtifactTTLError(msg)

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
        return bool(
            await self._client.exists(full_key)
            or await self._client.exists(self._legacy_full_key(key))
        )

    async def cleanup_run(self, run_id: str, *, idempotency_key: str) -> int:
        """Remove all artifacts for a run using scan_iter.

        Scans for all keys matching the run_id prefix and deletes them.

        Args:
            run_id: Run identifier whose artifacts should be cleaned up.
            idempotency_key: Stable operation identifier used for replay receipts.

        Returns:
            Count of deleted logical artifacts.
        """
        pattern = f"{self._prefix}:data:{run_id}/*"
        legacy_pattern = f"{self._prefix}:{run_id}/*"
        has_receipt, stable_count = await self._existing_receipt(
            idempotency_key,
            kind="cleanup_run",
            target=run_id,
            result_type=int,
        )
        if has_receipt:
            return int(stable_count)
        redis_keys = [key async for key in self._client.scan_iter(match=pattern, count=100)]
        legacy_keys = [key async for key in self._client.scan_iter(match=legacy_pattern, count=100)]

        def _text(redis_key: str | bytes) -> str:
            return redis_key.decode() if isinstance(redis_key, bytes) else redis_key

        new_prefix = f"{self._prefix}:data:"
        legacy_prefix = f"{self._prefix}:"
        logical_keys = {_text(key).removeprefix(new_prefix) for key in redis_keys}
        legacy_text = {_text(key) for key in legacy_keys}
        logical_keys.update(
            key.removeprefix(legacy_prefix) for key in legacy_text if f"{key}:meta" in legacy_text
        )
        count = len(logical_keys)
        claimed, stable_count = await self._claim_receipt(
            idempotency_key,
            kind="cleanup_run",
            target=run_id,
            result=count,
            result_type=int,
        )
        if not claimed:
            return int(stable_count)
        for redis_key in redis_keys:
            await self._client.delete(redis_key)
            redis_key_text = redis_key.decode() if isinstance(redis_key, bytes) else redis_key
            await self._client.delete(
                redis_key_text.replace(f"{self._prefix}:data:", f"{self._prefix}:meta:", 1)
            )
        for redis_key in legacy_keys:
            await self._client.delete(redis_key)
        return stable_count


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
        required_dir_fd = {os.open, os.mkdir, os.stat, os.unlink, os.rmdir}
        if (
            fcntl is None
            or not all(hasattr(os, name) for name in ("O_NOFOLLOW", "O_DIRECTORY"))
            or not required_dir_fd.issubset(os.supports_dir_fd)
        ):
            raise ArtifactStorageError("Descriptor-safe artifact storage is unsupported")
        self._base_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._base_fd = os.open(
                self._base_dir,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
        except OSError:
            raise ArtifactStorageError("Unable to safely open artifact base") from None
        self._default_ttl = default_ttl
        self._max_size = max_size

    def __del__(self) -> None:
        base_fd = getattr(self, "_base_fd", None)
        if base_fd is not None:
            with contextlib.suppress(OSError):
                os.close(base_fd)
            self._base_fd = None

    def _validate_key(self, key: str) -> None:
        """Reject keys that cannot be safely resolved below the artifact base.

        Args:
            key: Artifact key to validate.

        Raises:
            ArtifactStorageError: If the key contains '..' segments.
        """
        if ".." in key.split("/"):
            msg = f"Rejected key with path traversal: {key}"
            raise ArtifactStorageError(msg)
        if not key or "\x00" in key or Path(key).is_absolute():
            raise ArtifactStorageError("Rejected unsafe artifact key")

    def _key_parts(self, key: str) -> list[str]:
        self._validate_key(key)
        parts = key.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ArtifactStorageError("Rejected unsafe artifact key")
        return parts

    def _open_parent_fd(self, key: str, *, create: bool) -> tuple[int, str]:
        parts = self._key_parts(key)
        current = os.dup(self._base_fd)
        try:
            for component in parts[:-1]:
                try:
                    next_fd = os.open(
                        component,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=current,
                    )
                except FileNotFoundError:
                    if not create:
                        raise
                    with contextlib.suppress(FileExistsError):
                        os.mkdir(component, mode=0o700, dir_fd=current)
                    next_fd = os.open(
                        component,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=current,
                    )
                os.close(current)
                current = next_fd
            return current, parts[-1]
        except (OSError, RuntimeError) as exc:
            os.close(current)
            if isinstance(exc, FileNotFoundError):
                raise
            raise ArtifactStorageError("Unsafe artifact directory chain") from None

    @staticmethod
    def _read_fd(fd: int) -> bytes:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)

    def _read_bytes_at(self, key: str) -> bytes:
        try:
            parent_fd, leaf = self._open_parent_fd(key, create=False)
        except FileNotFoundError:
            raise ArtifactNotFoundError(f"Artifact not found: {key}") from None
        try:
            fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
            try:
                return self._read_fd(fd)
            finally:
                os.close(fd)
        except FileNotFoundError:
            raise ArtifactNotFoundError(f"Artifact not found: {key}") from None
        except OSError:
            raise ArtifactStorageError("Unsafe artifact file") from None
        finally:
            os.close(parent_fd)

    def _write_bytes_at(self, key: str, data: bytes) -> None:
        parent_fd, leaf = self._open_parent_fd(key, create=True)
        try:
            fd = os.open(
                leaf,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
            try:
                view = memoryview(data)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
            finally:
                os.close(fd)
        except OSError:
            raise ArtifactStorageError("Unsafe artifact file") from None
        finally:
            os.close(parent_fd)

    def _exists_at(self, key: str) -> bool:
        try:
            parent_fd, leaf = self._open_parent_fd(key, create=False)
        except FileNotFoundError:
            return False
        try:
            stat_result = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISREG(stat_result.st_mode):
                raise ArtifactStorageError("Unsafe artifact file")
            return True
        except FileNotFoundError:
            return False
        except OSError:
            raise ArtifactStorageError("Unsafe artifact file") from None
        finally:
            os.close(parent_fd)

    def _receipt_name(self, idempotency_key: str) -> str:
        self._validate_key(idempotency_key)
        if "/" in idempotency_key:
            raise ArtifactStorageError("Rejected unsafe idempotency key")
        return f"{idempotency_key}.json"

    @contextlib.contextmanager
    def _receipt_lock(self, idempotency_key: str):
        self._receipt_name(idempotency_key)
        lock_name = hashlib.sha256(idempotency_key.encode()).hexdigest() + ".lock"
        lock_dir_fd, _ = self._open_parent_fd(".erasure-locks/placeholder", create=True)
        try:
            lock_fd: int | None = None
            for attempt in range(3):
                try:
                    lock_fd = os.open(
                        lock_name,
                        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=lock_dir_fd,
                    )
                except FileNotFoundError:
                    if attempt == 2:
                        raise ArtifactStorageError("Unable to lock erasure receipt") from None
                    replacement_fd, _ = self._open_parent_fd(
                        ".erasure-locks/placeholder", create=True
                    )
                    os.close(lock_dir_fd)
                    lock_dir_fd = replacement_fd
                    continue
                except OSError:
                    raise ArtifactStorageError("Unable to lock erasure receipt") from None
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX)
                except OSError:
                    with contextlib.suppress(OSError):
                        os.close(lock_fd)
                    lock_fd = None
                    raise ArtifactStorageError("Unable to lock erasure receipt") from None
                break
            if lock_fd is None:
                raise ArtifactStorageError("Unable to lock erasure receipt")
            try:
                yield
            except BaseException:
                with contextlib.suppress(OSError):
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                raise
            else:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except OSError:
                    raise ArtifactStorageError("Unable to unlock erasure receipt") from None
            finally:
                with contextlib.suppress(OSError):
                    os.close(lock_fd)
        finally:
            os.close(lock_dir_fd)

    def _write_receipt(self, idempotency_key: str, payload: dict[str, Any]) -> None:
        """Persist a replay receipt atomically (temp file + rename)."""
        receipt_name = self._receipt_name(idempotency_key)
        receipt_dir_fd, _ = self._open_parent_fd(".erasure-receipts/placeholder", create=True)
        temporary = f".{hashlib.sha256(idempotency_key.encode()).hexdigest()}.{token_hex(8)}.tmp"
        try:
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=receipt_dir_fd,
            )
            try:
                payload_bytes = json.dumps(payload, sort_keys=True).encode()
                view = memoryview(payload_bytes)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(
                temporary,
                receipt_name,
                src_dir_fd=receipt_dir_fd,
                dst_dir_fd=receipt_dir_fd,
            )
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(temporary, dir_fd=receipt_dir_fd)
            raise ArtifactStorageError("Unable to safely write erasure receipt") from None
        finally:
            os.close(receipt_dir_fd)

    def _read_receipt(self, idempotency_key: str) -> dict[str, Any] | None:
        """Load a stored replay receipt, or None when the operation is new."""
        receipt_name = self._receipt_name(idempotency_key)
        try:
            receipt_dir_fd, _ = self._open_parent_fd(".erasure-receipts/placeholder", create=False)
        except FileNotFoundError:
            return None
        try:
            try:
                fd = os.open(receipt_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=receipt_dir_fd)
            except FileNotFoundError:
                return None
            try:
                return json.loads(self._read_fd(fd))
            finally:
                os.close(fd)
        except (OSError, ValueError):
            raise ArtifactStorageError("Unable to safely read erasure receipt") from None
        finally:
            os.close(receipt_dir_fd)

    def _write_file(self, key: str, data: bytes, meta: dict[str, Any]) -> None:
        """Synchronous file write for use with asyncio.to_thread."""
        self._write_bytes_at(key, data)
        self._write_bytes_at(f"{key}.meta.json", json.dumps(meta).encode())

    def _read_file(self, key: str) -> bytes:
        """Synchronous file read for use with asyncio.to_thread."""
        return self._read_bytes_at(key)

    def _read_meta(self, key: str) -> dict[str, Any]:
        """Synchronous metadata read for use with asyncio.to_thread."""
        try:
            return json.loads(self._read_bytes_at(f"{key}.meta.json"))
        except ArtifactNotFoundError:
            raise ArtifactNotFoundError(f"Artifact metadata not found: {key}") from None

    def _is_expired(self, meta: dict[str, Any]) -> bool:
        """Check if an artifact has expired based on its sidecar metadata."""
        expires_at_str = meta.get("expires_at")
        if expires_at_str is None:
            return False
        expires_at = datetime.fromisoformat(expires_at_str)
        return datetime.now(UTC) >= expires_at

    def _delete_files(self, key: str) -> bool:
        """Synchronous file deletion for use with asyncio.to_thread."""
        existed = self._exists_at(key)
        for target in (key, f"{key}.meta.json"):
            try:
                parent_fd, leaf = self._open_parent_fd(target, create=False)
            except FileNotFoundError:
                continue
            try:
                os.unlink(leaf, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            except OSError:
                raise ArtifactStorageError("Unable to safely delete artifact") from None
            finally:
                os.close(parent_fd)
        return existed

    def _count_artifacts_fd(self, directory_fd: int) -> int:
        count = 0
        for name in os.listdir(directory_fd):
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
                try:
                    count += self._count_artifacts_fd(child_fd)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(info.st_mode):
                count += int(not name.endswith(".meta.json"))
            else:
                raise ArtifactStorageError("Unsafe entry in artifact tree")
        return count

    def _remove_tree_fd(self, directory_fd: int) -> None:
        for name in os.listdir(directory_fd):
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
                try:
                    self._remove_tree_fd(child_fd)
                finally:
                    os.close(child_fd)
                os.rmdir(name, dir_fd=directory_fd)
            elif stat.S_ISREG(info.st_mode):
                os.unlink(name, dir_fd=directory_fd)
            else:
                raise ArtifactStorageError("Unsafe entry in artifact tree")

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
            key: Logical artifact key in a cleanup-compatible grammar.
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

    async def delete(self, key: str, *, idempotency_key: str) -> bool:
        """Delete an artifact and its sidecar.

        Args:
            key: Artifact key to delete.
            idempotency_key: Stable operation identifier used for replay receipts.

        Returns:
            True if the artifact existed and was deleted, False otherwise.
        """
        self._validate_key(key)

        def _delete() -> bool:
            """Synchronous idempotent delete for use with asyncio.to_thread."""
            with self._receipt_lock(idempotency_key):
                receipt = self._read_receipt(idempotency_key)
                if receipt is None:
                    result = self._exists_at(key)
                    self._write_receipt(
                        idempotency_key,
                        {"kind": "delete", "target": key, "result": result},
                    )
                else:
                    if receipt.get("kind") != "delete" or receipt.get("target") != key:
                        raise ArtifactStorageError("idempotency key reused for another operation")
                    result = bool(receipt["result"])
                self._delete_files(key)
                return result

        return await asyncio.to_thread(_delete)

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

        def _refresh() -> bool:
            """Synchronous sidecar TTL rewrite for use with asyncio.to_thread."""
            try:
                meta = self._read_meta(key)
            except ArtifactNotFoundError:
                msg = f"Cannot refresh TTL for missing artifact: {key}"
                raise ArtifactTTLError(msg) from None
            now = datetime.now(UTC)
            meta["ttl_seconds"] = ttl
            meta["expires_at"] = (now + timedelta(seconds=ttl)).isoformat()
            self._write_bytes_at(f"{key}.meta.json", json.dumps(meta).encode())
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
            """Synchronous existence + expiry check for use with asyncio.to_thread."""
            if not self._exists_at(key):
                return False
            try:
                meta = self._read_meta(key)
            except ArtifactNotFoundError:
                return False
            return not self._is_expired(meta)

        return await asyncio.to_thread(_check)

    async def cleanup_run(self, run_id: str, *, idempotency_key: str) -> int:
        """Remove all artifacts for a run by deleting the run directory tree.

        Args:
            run_id: Run identifier whose artifacts should be cleaned up.
            idempotency_key: Stable operation identifier used for replay receipts.

        Returns:
            Count of deleted artifact files (excluding sidecars).
        """

        def _cleanup() -> int:
            """Synchronous idempotent run-tree removal for use with asyncio.to_thread."""
            with self._receipt_lock(idempotency_key):
                receipt = self._read_receipt(idempotency_key)
                if receipt is not None:
                    if receipt.get("kind") != "cleanup_run" or receipt.get("target") != run_id:
                        raise ArtifactStorageError("idempotency key reused for another operation")
                    count = int(receipt["result"])
                try:
                    parent_fd, leaf = self._open_parent_fd(run_id, create=False)
                except FileNotFoundError:
                    parent_fd = None
                    leaf = ""
                if receipt is None:
                    count = 0
                    if parent_fd is not None:
                        try:
                            run_fd = os.open(
                                leaf,
                                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=parent_fd,
                            )
                            try:
                                count = self._count_artifacts_fd(run_fd)
                            finally:
                                os.close(run_fd)
                        except FileNotFoundError:
                            pass
                        except OSError:
                            raise ArtifactStorageError("Unsafe artifact run tree") from None
                    self._write_receipt(
                        idempotency_key,
                        {"kind": "cleanup_run", "target": run_id, "result": count},
                    )
                if parent_fd is not None:
                    try:
                        run_fd = os.open(
                            leaf,
                            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=parent_fd,
                        )
                        try:
                            self._remove_tree_fd(run_fd)
                        finally:
                            os.close(run_fd)
                        os.rmdir(leaf, dir_fd=parent_fd)
                    except FileNotFoundError:
                        pass
                    except OSError:
                        raise ArtifactStorageError("Unsafe artifact run tree") from None
                    finally:
                        os.close(parent_fd)
                return count

        return await asyncio.to_thread(_cleanup)
