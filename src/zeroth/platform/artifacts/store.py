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
    """Redis-backed artifact store with atomic Lua erasure receipts.

    Stores artifact data and metadata as separate Redis keys with optional
    TTL. Run cleanup uses ``KEYS`` inside a Lua script so enumeration, deletion,
    and receipt completion cannot interleave. This blocks the Redis server for
    the namespace scan and requires standalone Redis 7+ (for command ACL
    preflight), not Redis Cluster.

    Args:
        redis_url: Redis connection URL.
        prefix: Key prefix for namespace isolation.
        default_ttl: Default TTL in seconds when none is specified.
        max_size: Maximum artifact payload size in bytes.
    """

    _ERASURE_SCRIPT = r"""
local kind = ARGV[1]
local target = ARGV[2]
local max_delete_keys = 1000

if type(redis.acl_check_cmd) ~= "function" then
    return {"unsupported"}
end
if not redis.acl_check_cmd("GET", KEYS[1])
    or not redis.acl_check_cmd("GET", KEYS[2]) then
    return {"forbidden"}
end

local function validate_receipt(raw, prior)
    local decoded, receipt = pcall(cjson.decode, raw)
    if not decoded or type(receipt) ~= "table" then
        if prior then
            return "legacy", nil
        end
        return "invalid", nil
    end
    if prior then
        if receipt.version ~= nil and receipt.version ~= 2 then
            return "mismatch", nil
        end
    elseif receipt.version ~= 2 then
        return "mismatch", nil
    end
    if receipt.kind ~= kind or receipt.target ~= target then
        return "mismatch", nil
    end
    if kind == "delete" then
        if type(receipt.result) ~= "boolean" then
            return "invalid", nil
        end
    elseif kind == "cleanup_run" then
        if type(receipt.result) ~= "number"
            or receipt.result < 0
            or receipt.result ~= math.floor(receipt.result) then
            return "invalid", nil
        end
    else
        return "invalid", nil
    end
    return "ok", receipt.result
end

local completed = redis.call("GET", KEYS[1])
if completed then
    local status, result = validate_receipt(completed, false)
    if status ~= "ok" then
        return {status}
    end
    return {"ok", cjson.encode(result)}
end

local prior = redis.call("GET", KEYS[2])
if prior then
    local status, result = validate_receipt(prior, true)
    if status ~= "ok" then
        return {status}
    end
    local migrated = cjson.encode({
        version = 2,
        kind = kind,
        target = target,
        result = result
    })
    if not redis.acl_check_cmd("SET", KEYS[1], migrated) then
        return {"forbidden"}
    end
    redis.call("SET", KEYS[1], migrated)
    return {"ok", cjson.encode(result)}
end

if kind == "delete" then
    local legacy_enabled = ARGV[3] == "1"
    if not redis.acl_check_cmd("EXISTS", KEYS[3]) then
        return {"forbidden"}
    end
    if legacy_enabled and not redis.acl_check_cmd("EXISTS", KEYS[5]) then
        return {"forbidden"}
    end
    local existed = redis.call("EXISTS", KEYS[3]) == 1
    if not existed and legacy_enabled then
        existed = redis.call("EXISTS", KEYS[5]) == 1
    end
    local delete_keys = {KEYS[3], KEYS[4]}
    if legacy_enabled then
        table.insert(delete_keys, KEYS[5])
        table.insert(delete_keys, KEYS[6])
    end
    local payload = cjson.encode({
        version = 2,
        kind = kind,
        target = target,
        result = existed
    })
    if not redis.acl_check_cmd("SET", KEYS[1], payload)
        or not redis.acl_check_cmd("DEL", unpack(delete_keys)) then
        return {"forbidden"}
    end
    -- Every fallible read, decode, allocation, and permission check is complete.
    -- SET can fail before deletion; bounded DEL is type-agnostic, frees memory,
    -- and is deliberately the script's final Redis command.
    local response = {"ok", cjson.encode(existed)}
    redis.call("SET", KEYS[1], payload)
    redis.call("DEL", unpack(delete_keys))
    return response
end

if kind ~= "cleanup_run" then
    return {"invalid"}
end

local new_exact = ARGV[3]
local meta_namespace = ARGV[4]
local legacy_exact = ARGV[5]
local new_namespace = ARGV[6]
local legacy_namespace = ARGV[7]
local legacy_enabled = ARGV[8] == "1"
if not redis.acl_check_cmd("KEYS", ARGV[9]) then
    return {"forbidden"}
end
if legacy_enabled and not redis.acl_check_cmd("KEYS", ARGV[10]) then
    return {"forbidden"}
end
local new_keys = redis.call("KEYS", ARGV[9])
local legacy_keys = {}
if legacy_enabled then
    legacy_keys = redis.call("KEYS", ARGV[10])
end
if #new_keys + #legacy_keys > max_delete_keys then
    return {"too_many"}
end
local logical = {}
local delete_keys = {}
local delete_seen = {}

local function add_delete(key)
    if not delete_seen[key] then
        delete_seen[key] = true
        table.insert(delete_keys, key)
    end
end

for _, key in ipairs(new_keys) do
    if string.sub(key, 1, string.len(new_exact)) == new_exact then
        local logical_key = string.sub(key, string.len(new_namespace) + 1)
        logical[logical_key] = true
        add_delete(key)
        add_delete(meta_namespace .. logical_key)
    end
end
for _, key in ipairs(legacy_keys) do
    if string.sub(key, 1, string.len(legacy_exact)) == legacy_exact then
        local logical_key = string.sub(key, string.len(legacy_namespace) + 1)
        if not redis.acl_check_cmd("EXISTS", key .. ":meta") then
            return {"forbidden"}
        end
        if redis.call("EXISTS", key .. ":meta") == 1 then
            logical[logical_key] = true
        end
        add_delete(key)
    end
end
if #delete_keys > max_delete_keys then
    return {"too_many"}
end

local count = 0
for _ in pairs(logical) do
    count = count + 1
end
local payload = cjson.encode({
    version = 2,
    kind = kind,
    target = target,
    result = count
})
if not redis.acl_check_cmd("SET", KEYS[1], payload) then
    return {"forbidden"}
end
if #delete_keys > 0 and not redis.acl_check_cmd("DEL", unpack(delete_keys)) then
    return {"forbidden"}
end
-- As above, the single bounded DEL is the final Redis command. Redis scripts
-- prevent another client (including ACL administration) from interleaving.
local response = {"ok", cjson.encode(count)}
redis.call("SET", KEYS[1], payload)
if #delete_keys > 0 then
    redis.call("DEL", unpack(delete_keys))
end
return response
"""
    _LEGACY_RESERVED_RUN_PREFIXES = ("data:", "meta:", "erasure-receipt:")

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

    def _legacy_key_is_unambiguous(self, key: str) -> bool:
        owner = key.partition("/")[0]
        return not owner.startswith(self._LEGACY_RESERVED_RUN_PREFIXES)

    @staticmethod
    def _glob_prefix(value: str) -> str:
        """Escape a literal Redis glob prefix before appending a wildcard."""
        return "".join(
            f"\\{character}" if character in r"\*?[]" else character for character in value
        )

    async def _run_erasure_script(
        self,
        idempotency_key: str,
        *,
        kind: str,
        target: str,
        result_type: type[bool] | type[int],
        keys: list[str],
        arguments: list[str] | None = None,
    ) -> bool | int:
        """Run receipt validation and destructive mutation as one Redis script."""
        script_keys = [
            self._receipt_key(idempotency_key),
            self._legacy_receipt_key(idempotency_key),
            *keys,
        ]
        try:
            response = await self._client.eval(
                self._ERASURE_SCRIPT,
                len(script_keys),
                *script_keys,
                kind,
                target,
                *(arguments or []),
            )
        except Exception:
            raise ArtifactStorageError("Redis erasure operation failed") from None
        if not isinstance(response, (list, tuple)) or not response:
            raise ArtifactStorageError("Invalid erasure operation receipt")
        raw_status = response[0]
        status = raw_status.decode() if isinstance(raw_status, bytes) else raw_status
        if status == "mismatch":
            raise ArtifactStorageError("idempotency key reused for another operation")
        if status == "legacy":
            raise ArtifactStorageError("Legacy erasure receipt blocks unbound replay")
        if status in {"forbidden", "too_many", "unsupported"}:
            raise ArtifactStorageError("Redis erasure operation failed")
        if status != "ok" or len(response) != 2:
            raise ArtifactStorageError("Invalid erasure operation receipt")
        raw_result = response[1]
        try:
            result = json.loads(raw_result)
        except (TypeError, ValueError):
            raise ArtifactStorageError("Invalid erasure operation receipt") from None
        if result_type is bool:
            if type(result) is not bool:
                raise ArtifactStorageError("Invalid erasure operation receipt")
        elif type(result) is not int or result < 0:
            raise ArtifactStorageError("Invalid erasure operation receipt")
        return result

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
        if data is None and self._legacy_key_is_unambiguous(key):
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
        result = await self._run_erasure_script(
            idempotency_key,
            kind="delete",
            target=key,
            result_type=bool,
            keys=[full_key, meta_key, legacy_full_key, legacy_meta_key],
            arguments=["1" if self._legacy_key_is_unambiguous(key) else "0"],
        )
        return bool(result)

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
        if not await self._client.exists(full_key) and self._legacy_key_is_unambiguous(key):
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
        if await self._client.exists(full_key):
            return True
        return bool(
            self._legacy_key_is_unambiguous(key)
            and await self._client.exists(self._legacy_full_key(key))
        )

    async def cleanup_run(self, run_id: str, *, idempotency_key: str) -> int:
        """Atomically remove all artifacts for a run and complete its receipt.

        The Lua script uses ``KEYS`` with escaped exact-prefix patterns. This
        deliberately trades server latency for a race-free cleanup boundary.
        At most 1,000 physical keys are removed per operation. Historical
        legacy runs beginning with a current Redis domain prefix are ambiguous
        and require offline migration; only their current-layout keys are
        cleaned here.

        Args:
            run_id: Run identifier whose artifacts should be cleaned up.
            idempotency_key: Stable operation identifier used for replay receipts.

        Returns:
            Count of deleted logical artifacts.
        """
        new_namespace = f"{self._prefix}:data:"
        meta_namespace = f"{self._prefix}:meta:"
        legacy_namespace = f"{self._prefix}:"
        new_exact = f"{new_namespace}{run_id}/"
        legacy_exact = f"{legacy_namespace}{run_id}/"
        legacy_enabled = not run_id.startswith(self._LEGACY_RESERVED_RUN_PREFIXES)
        stable_count = await self._run_erasure_script(
            idempotency_key,
            kind="cleanup_run",
            target=run_id,
            result_type=int,
            keys=[],
            arguments=[
                new_exact,
                meta_namespace,
                legacy_exact,
                new_namespace,
                legacy_namespace,
                "1" if legacy_enabled else "0",
                f"{self._glob_prefix(new_exact)}*",
                f"{self._glob_prefix(legacy_exact)}*",
            ],
        )
        return int(stable_count)


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
