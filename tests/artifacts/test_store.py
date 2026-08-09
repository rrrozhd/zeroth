"""Tests for ArtifactStore Protocol and Redis/Filesystem implementations."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zeroth.platform.artifacts.errors import (
    ArtifactNotFoundError,
    ArtifactStorageError,
    ArtifactTTLError,
)
from zeroth.platform.artifacts.models import ArtifactReference
from zeroth.platform.artifacts.store import (
    ArtifactStore,
    FilesystemArtifactStore,
    RedisArtifactStore,
)


# ---------------------------------------------------------------------------
# ArtifactStore Protocol tests
# ---------------------------------------------------------------------------


class TestArtifactStoreProtocol:
    """Tests for the ArtifactStore protocol definition."""

    def test_is_protocol(self) -> None:
        """ArtifactStore is a typing.Protocol."""
        assert issubclass(type(ArtifactStore), type(Protocol))

    def test_has_required_methods(self) -> None:
        """ArtifactStore defines store, retrieve, delete, refresh_ttl, exists, cleanup_run."""
        expected_methods = ["store", "retrieve", "delete", "refresh_ttl", "exists", "cleanup_run"]
        for method_name in expected_methods:
            assert hasattr(ArtifactStore, method_name), f"Missing method: {method_name}"


# ---------------------------------------------------------------------------
# RedisArtifactStore tests
# ---------------------------------------------------------------------------


class TestRedisArtifactStore:
    """Tests for the Redis-backed artifact store."""

    @pytest.fixture()
    def mock_redis(self) -> MagicMock:
        """Create a mock Redis async client.

        Uses MagicMock for the top level because redis.asyncio.Redis.pipeline()
        is a synchronous call returning an async context manager, not a coroutine.
        Individual async methods (get, exists, delete) are set up as AsyncMock.
        """
        client = MagicMock()

        # Pipeline mock as async context manager
        pipeline = MagicMock()
        pipeline.setex = MagicMock()
        pipeline.set = MagicMock()
        pipeline.delete = MagicMock()
        pipeline.expire = MagicMock()
        pipeline.execute = AsyncMock(return_value=[True, True])
        pipeline.__aenter__ = AsyncMock(return_value=pipeline)
        pipeline.__aexit__ = AsyncMock(return_value=False)
        client.pipeline.return_value = pipeline

        # Async methods on the client
        client.get = AsyncMock(return_value=None)
        client.set = AsyncMock(return_value=True)
        client.exists = AsyncMock(return_value=0)
        client.delete = AsyncMock(return_value=1)
        client.scan_iter = MagicMock()

        return client

    @pytest.fixture()
    def store(self, mock_redis: MagicMock) -> RedisArtifactStore:
        """Create a RedisArtifactStore with mocked client."""
        return RedisArtifactStore(
            redis_url="redis://localhost:6379/0",
            prefix="zeroth:artifact",
            default_ttl=3600,
            max_size=104857600,
            client=mock_redis,
        )

    @pytest.mark.asyncio()
    async def test_store_with_ttl(self, store: RedisArtifactStore, mock_redis: MagicMock) -> None:
        """store() with TTL calls pipeline with two setex operations."""
        pipeline = mock_redis.pipeline.return_value
        pipeline.execute.return_value = [True, True]

        ref = await store.store("run1/node1/abc123", b"hello", "text/plain", ttl=600)

        assert isinstance(ref, ArtifactReference)
        assert ref.store == "redis"
        assert ref.key == "run1/node1/abc123"
        assert ref.content_type == "text/plain"
        assert ref.size == 5
        assert ref.ttl_seconds == 600

        # Verify pipeline was used with setex for both data and meta
        pipeline.setex.assert_called()
        assert pipeline.setex.call_count == 2

    @pytest.mark.asyncio()
    async def test_store_without_ttl(
        self, store: RedisArtifactStore, mock_redis: MagicMock
    ) -> None:
        """store() without TTL calls pipeline with two set operations (no TTL)."""
        pipeline = mock_redis.pipeline.return_value
        pipeline.execute.return_value = [True, True]

        ref = await store.store("run1/node1/abc123", b"data", "application/json")

        assert ref.ttl_seconds is None

        # Verify pipeline used set (not setex) for both keys
        pipeline.set.assert_called()
        assert pipeline.set.call_count == 2

    @pytest.mark.asyncio()
    async def test_store_rejects_oversized_payload(self, store: RedisArtifactStore) -> None:
        """store() rejects payload exceeding max_artifact_size_bytes."""
        store._max_size = 10  # 10 bytes max
        with pytest.raises(ArtifactStorageError, match="exceeds maximum"):
            await store.store("run1/node1/abc", b"x" * 11, "text/plain")

    @pytest.mark.asyncio()
    async def test_retrieve_existing(
        self, store: RedisArtifactStore, mock_redis: MagicMock
    ) -> None:
        """retrieve() returns bytes for existing key."""
        mock_redis.get.return_value = b"file-contents"

        result = await store.retrieve("run1/node1/abc123")

        assert result == b"file-contents"
        mock_redis.get.assert_called_once_with("zeroth:artifact:data:run1/node1/abc123")

    @pytest.mark.asyncio()
    async def test_retrieve_missing(self, store: RedisArtifactStore, mock_redis: MagicMock) -> None:
        """retrieve() raises ArtifactNotFoundError for missing key."""
        mock_redis.get.return_value = None

        with pytest.raises(ArtifactNotFoundError):
            await store.retrieve("run1/node1/missing")

    @pytest.mark.asyncio()
    async def test_delete_existing(self, store: RedisArtifactStore, mock_redis: MagicMock) -> None:
        """delete() returns True for existing key."""
        pipeline = mock_redis.pipeline.return_value
        pipeline.execute.return_value = [1, 1]
        mock_redis.exists.return_value = 1

        result = await store.delete("run1/node1/abc123", idempotency_key="delete-existing")

        assert result is True

    @pytest.mark.asyncio()
    async def test_delete_missing(self, store: RedisArtifactStore, mock_redis: MagicMock) -> None:
        """delete() returns False for missing key."""
        pipeline = mock_redis.pipeline.return_value
        pipeline.execute.return_value = [0, 0]

        result = await store.delete("run1/node1/missing", idempotency_key="delete-missing")

        assert result is False

    @pytest.mark.asyncio()
    async def test_delete_replay_returns_stable_receipt(
        self, store: RedisArtifactStore, mock_redis: MagicMock
    ) -> None:
        receipts: dict[str, bytes] = {}

        async def get(key: str):
            return receipts.get(key)

        async def set_value(key: str, value: str, *, nx: bool = False):
            if not nx or key not in receipts:
                receipts[key] = value.encode()
                return True
            return False

        mock_redis.get.side_effect = get
        mock_redis.set.side_effect = set_value
        mock_redis.exists.side_effect = [1, 0]
        pipeline = mock_redis.pipeline.return_value
        pipeline.execute.return_value = [1, 1]

        first = await store.delete("run1/node1/replay", idempotency_key="redis-replay")
        second = await store.delete("run1/node1/replay", idempotency_key="redis-replay")

        assert first is True and second is True

    @pytest.mark.asyncio()
    async def test_refresh_ttl_existing(
        self, store: RedisArtifactStore, mock_redis: MagicMock
    ) -> None:
        """refresh_ttl() pipelines expire on both keys, returns True if key exists."""
        mock_redis.exists.return_value = 1
        pipeline = mock_redis.pipeline.return_value
        pipeline.execute.return_value = [True, True]

        result = await store.refresh_ttl("run1/node1/abc123", 1200)

        assert result is True
        pipeline.expire.assert_called()
        assert pipeline.expire.call_count == 2

    @pytest.mark.asyncio()
    async def test_refresh_ttl_missing(
        self, store: RedisArtifactStore, mock_redis: MagicMock
    ) -> None:
        """refresh_ttl() raises ArtifactTTLError when key does not exist."""
        mock_redis.exists.return_value = 0

        with pytest.raises(ArtifactTTLError):
            await store.refresh_ttl("run1/node1/missing", 600)

    @pytest.mark.asyncio()
    async def test_exists_true(self, store: RedisArtifactStore, mock_redis: MagicMock) -> None:
        """exists() returns True based on redis exists command."""
        mock_redis.exists.return_value = 1

        result = await store.exists("run1/node1/abc123")

        assert result is True

    @pytest.mark.asyncio()
    async def test_exists_false(self, store: RedisArtifactStore, mock_redis: MagicMock) -> None:
        """exists() returns False when key missing."""
        mock_redis.exists.return_value = 0

        result = await store.exists("run1/node1/missing")

        assert result is False

    @pytest.mark.asyncio()
    async def test_cleanup_run(self, store: RedisArtifactStore, mock_redis: MagicMock) -> None:
        """cleanup_run() uses scan_iter with prefix pattern, deletes matching keys."""
        mock_redis.scan_iter.side_effect = [
            self._async_iter([b"zeroth:artifact:data:run1/a", b"zeroth:artifact:data:run1/b"]),
            self._async_iter([]),
        ]
        mock_redis.delete.return_value = 1

        count = await store.cleanup_run("run1", idempotency_key="cleanup-run1")

        assert count == 2
        assert mock_redis.scan_iter.call_count == 2
        # Verify the scan pattern contains the run_id
        call_kwargs = mock_redis.scan_iter.call_args
        assert "run1" in str(call_kwargs)

    @staticmethod
    async def _async_iter(items: list) -> ...:
        """Helper to create an async iterator from a list."""
        for item in items:
            yield item


# ---------------------------------------------------------------------------
# FilesystemArtifactStore tests
# ---------------------------------------------------------------------------


class TestFilesystemArtifactStore:
    """Tests for the filesystem-backed artifact store."""

    @pytest.fixture()
    def store(self, tmp_path: Path) -> FilesystemArtifactStore:
        """Create a FilesystemArtifactStore using tmp_path."""
        return FilesystemArtifactStore(
            base_dir=tmp_path,
            default_ttl=3600,
            max_size=104857600,
        )

    @pytest.mark.asyncio()
    async def test_store_creates_file_and_sidecar(
        self, store: FilesystemArtifactStore, tmp_path: Path
    ) -> None:
        """store() creates file and .meta.json sidecar with correct content."""
        ref = await store.store("run1/node1/abc123", b"file data", "text/plain", ttl=600)

        assert isinstance(ref, ArtifactReference)
        assert ref.store == "filesystem"
        assert ref.key == "run1/node1/abc123"
        assert ref.content_type == "text/plain"
        assert ref.size == 9
        assert ref.ttl_seconds == 600

        # Verify file exists
        file_path = tmp_path / "run1" / "node1" / "abc123"
        assert file_path.exists()
        assert file_path.read_bytes() == b"file data"

        # Verify sidecar exists
        meta_path = tmp_path / "run1" / "node1" / "abc123.meta.json"
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text())
        assert meta["content_type"] == "text/plain"
        assert meta["size"] == 9
        assert meta["ttl_seconds"] == 600

    @pytest.mark.asyncio()
    async def test_store_creates_parent_directories(
        self, store: FilesystemArtifactStore, tmp_path: Path
    ) -> None:
        """store() creates parent directories with mkdir parents=True."""
        await store.store("deep/nested/run/node/key123", b"data", "text/plain")

        file_path = tmp_path / "deep" / "nested" / "run" / "node" / "key123"
        assert file_path.exists()

    @pytest.mark.asyncio()
    async def test_store_rejects_oversized_payload(self, store: FilesystemArtifactStore) -> None:
        """store() rejects payload exceeding max_artifact_size_bytes."""
        store._max_size = 10
        with pytest.raises(ArtifactStorageError, match="exceeds maximum"):
            await store.store("run1/node1/abc", b"x" * 11, "text/plain")

    @pytest.mark.asyncio()
    async def test_retrieve_existing(self, store: FilesystemArtifactStore, tmp_path: Path) -> None:
        """retrieve() returns file contents for existing artifact."""
        await store.store("run1/node1/abc123", b"stored data", "text/plain", ttl=3600)

        result = await store.retrieve("run1/node1/abc123")

        assert result == b"stored data"

    @pytest.mark.asyncio()
    async def test_retrieve_missing(self, store: FilesystemArtifactStore) -> None:
        """retrieve() raises ArtifactNotFoundError for missing file."""
        with pytest.raises(ArtifactNotFoundError):
            await store.retrieve("run1/node1/nonexistent")

    @pytest.mark.asyncio()
    async def test_retrieve_expired(self, store: FilesystemArtifactStore, tmp_path: Path) -> None:
        """retrieve() raises ArtifactNotFoundError for expired artifact."""
        # Store with very short TTL
        store._default_ttl = 1
        await store.store("run1/node1/expired", b"old data", "text/plain", ttl=1)

        # Manually set the expires_at to the past in the sidecar
        meta_path = tmp_path / "run1" / "node1" / "expired.meta.json"
        meta = json.loads(meta_path.read_text())
        meta["expires_at"] = "2020-01-01T00:00:00+00:00"
        meta_path.write_text(json.dumps(meta))

        with pytest.raises(ArtifactNotFoundError):
            await store.retrieve("run1/node1/expired")

    @pytest.mark.asyncio()
    async def test_delete_existing(self, store: FilesystemArtifactStore, tmp_path: Path) -> None:
        """delete() removes both file and sidecar, returns True."""
        await store.store("run1/node1/abc123", b"data", "text/plain")

        result = await store.delete("run1/node1/abc123", idempotency_key="fs-delete")

        assert result is True
        assert not (tmp_path / "run1" / "node1" / "abc123").exists()
        assert not (tmp_path / "run1" / "node1" / "abc123.meta.json").exists()

    @pytest.mark.asyncio()
    async def test_delete_missing(self, store: FilesystemArtifactStore) -> None:
        """delete() returns False for missing file."""
        result = await store.delete("run1/node1/nonexistent", idempotency_key="fs-missing")

        assert result is False

    @pytest.mark.asyncio()
    async def test_refresh_ttl_updates_sidecar(
        self, store: FilesystemArtifactStore, tmp_path: Path
    ) -> None:
        """refresh_ttl() updates expires_at in sidecar."""
        await store.store("run1/node1/abc123", b"data", "text/plain", ttl=60)

        result = await store.refresh_ttl("run1/node1/abc123", 7200)

        assert result is True

        meta_path = tmp_path / "run1" / "node1" / "abc123.meta.json"
        meta = json.loads(meta_path.read_text())
        assert meta["ttl_seconds"] == 7200
        # expires_at should be in the future
        expires_at = datetime.fromisoformat(meta["expires_at"])
        assert expires_at > datetime.now(UTC)

    @pytest.mark.asyncio()
    async def test_refresh_ttl_missing_raises(self, store: FilesystemArtifactStore) -> None:
        """refresh_ttl() raises ArtifactTTLError for missing artifact."""
        with pytest.raises(ArtifactTTLError):
            await store.refresh_ttl("run1/node1/nonexistent", 600)

    @pytest.mark.asyncio()
    async def test_exists_true(self, store: FilesystemArtifactStore, tmp_path: Path) -> None:
        """exists() returns True for existing non-expired artifact."""
        await store.store("run1/node1/abc123", b"data", "text/plain", ttl=3600)

        result = await store.exists("run1/node1/abc123")

        assert result is True

    @pytest.mark.asyncio()
    async def test_exists_false_missing(self, store: FilesystemArtifactStore) -> None:
        """exists() returns False for missing artifact."""
        result = await store.exists("run1/node1/nonexistent")

        assert result is False

    @pytest.mark.asyncio()
    async def test_exists_false_expired(
        self, store: FilesystemArtifactStore, tmp_path: Path
    ) -> None:
        """exists() returns False for expired artifact."""
        await store.store("run1/node1/expired", b"data", "text/plain", ttl=1)

        # Manually expire it
        meta_path = tmp_path / "run1" / "node1" / "expired.meta.json"
        meta = json.loads(meta_path.read_text())
        meta["expires_at"] = "2020-01-01T00:00:00+00:00"
        meta_path.write_text(json.dumps(meta))

        result = await store.exists("run1/node1/expired")

        assert result is False

    @pytest.mark.asyncio()
    async def test_cleanup_run(self, store: FilesystemArtifactStore, tmp_path: Path) -> None:
        """cleanup_run() removes entire {base_dir}/{run_id}/ directory tree."""
        await store.store("myrun/node1/file1", b"data1", "text/plain")
        await store.store("myrun/node2/file2", b"data2", "text/plain")
        await store.store("otherrun/node1/file3", b"data3", "text/plain")

        count = await store.cleanup_run("myrun", idempotency_key="fs-cleanup")

        assert count > 0
        assert not (tmp_path / "myrun").exists()
        # Other runs should be untouched
        assert (tmp_path / "otherrun").exists()

    @pytest.mark.asyncio()
    async def test_delete_replay_returns_stable_logical_result(
        self, store: FilesystemArtifactStore
    ) -> None:
        await store.store("run1/node1/replay", b"data", "text/plain")

        first = await store.delete("run1/node1/replay", idempotency_key="op-replay")
        second = await store.delete("run1/node1/replay", idempotency_key="op-replay")

        assert first is True and second is True

    @pytest.mark.asyncio()
    async def test_cleanup_replay_returns_stable_logical_count(
        self, store: FilesystemArtifactStore
    ) -> None:
        await store.store("replay-run/n1/a", b"a", "text/plain")
        await store.store("replay-run/n2/b", b"b", "text/plain")

        first = await store.cleanup_run("replay-run", idempotency_key="cleanup-replay")
        second = await store.cleanup_run("replay-run", idempotency_key="cleanup-replay")

        assert first == second == 2

    @pytest.mark.asyncio()
    async def test_path_traversal_rejected(self, store: FilesystemArtifactStore) -> None:
        """Key containing '..' raises ArtifactStorageError (path traversal prevention)."""
        with pytest.raises(ArtifactStorageError, match="path traversal"):
            await store.store("run1/../../../etc/passwd", b"evil", "text/plain")

    @pytest.mark.asyncio()
    @pytest.mark.parametrize("key", ["/tmp/absolute-artifact", "run1/node1/bad\x00key"])
    async def test_unsafe_key_rejected(self, store: FilesystemArtifactStore, key: str) -> None:
        with pytest.raises(ArtifactStorageError, match="unsafe artifact key"):
            await store.store(key, b"evil", "text/plain")

    @pytest.mark.asyncio()
    async def test_data_symlink_escape_rejected_before_write(
        self, store: FilesystemArtifactStore, tmp_path: Path
    ) -> None:
        outside = tmp_path.parent / f"{tmp_path.name}-outside-data"
        outside.mkdir()
        (tmp_path / "run1").symlink_to(outside, target_is_directory=True)

        with pytest.raises(ArtifactStorageError, match="Unsafe artifact"):
            await store.store("run1/node1/key", b"evil", "text/plain")

        assert not (outside / "node1" / "key").exists()

    @pytest.mark.asyncio()
    async def test_metadata_symlink_escape_rejected_before_read(
        self, store: FilesystemArtifactStore, tmp_path: Path
    ) -> None:
        await store.store("run1/node1/key", b"inside", "text/plain")
        meta_path = tmp_path / "run1" / "node1" / "key.meta.json"
        outside = tmp_path.parent / f"{tmp_path.name}-outside-meta.json"
        outside.write_text(meta_path.read_text())
        meta_path.unlink()
        meta_path.symlink_to(outside)

        with pytest.raises(ArtifactStorageError, match="Unsafe artifact"):
            await store.retrieve("run1/node1/key")

    @pytest.mark.asyncio()
    async def test_receipt_symlink_escape_rejected_before_write(
        self, store: FilesystemArtifactStore, tmp_path: Path
    ) -> None:
        await store.store("run1/node1/key", b"inside", "text/plain")
        outside = tmp_path.parent / f"{tmp_path.name}-outside-receipts"
        outside.mkdir()
        (tmp_path / ".erasure-receipts").symlink_to(outside, target_is_directory=True)

        with pytest.raises(ArtifactStorageError, match="Unsafe artifact"):
            await store.delete("run1/node1/key", idempotency_key="delete-key")

        assert not (outside / "delete-key.json").exists()

    @pytest.mark.asyncio()
    async def test_symlink_loop_is_rejected_as_storage_error(
        self, store: FilesystemArtifactStore, tmp_path: Path
    ) -> None:
        (tmp_path / "loop").symlink_to(tmp_path / "loop")

        with pytest.raises(ArtifactStorageError, match="Unsafe artifact"):
            await store.exists("loop/key")

    @pytest.mark.asyncio()
    async def test_base_swap_cannot_redirect_any_filesystem_operation(self, tmp_path: Path) -> None:
        base = tmp_path / "base"
        outside = tmp_path / "outside"
        outside.mkdir()
        store = FilesystemArtifactStore(base)
        await store.store("run/node/key", b"inside", "text/plain", ttl=60)
        original = tmp_path / "original-base"
        base.rename(original)
        base.symlink_to(outside, target_is_directory=True)

        assert await store.retrieve("run/node/key") == b"inside"
        assert await store.refresh_ttl("run/node/key", 120)
        assert await store.delete("run/node/key", idempotency_key="delete") is True
        await store.store("other/node/key", b"new", "text/plain")

        assert not list(outside.rglob("*"))
        assert (original / "other" / "node" / "key").read_bytes() == b"new"
        assert (original / ".erasure-receipts" / "delete.json").exists()

    @pytest.mark.asyncio()
    async def test_filesystem_receipt_competition_has_one_binding_winner(
        self, tmp_path: Path
    ) -> None:
        first = FilesystemArtifactStore(tmp_path)
        second = FilesystemArtifactStore(tmp_path)
        await first.store("run/a", b"a", "text/plain")
        await first.store("run/b", b"b", "text/plain")

        results = await asyncio.gather(
            first.delete("run/a", idempotency_key="contended"),
            second.cleanup_run("run", idempotency_key="contended"),
            return_exceptions=True,
        )

        winners = [
            index for index, result in enumerate(results) if not isinstance(result, Exception)
        ]
        losers = [result for result in results if isinstance(result, Exception)]
        assert len(winners) == len(losers) == 1
        assert isinstance(losers[0], ArtifactStorageError)
        assert str(losers[0]) == "idempotency key reused for another operation"
        restarted = FilesystemArtifactStore(tmp_path)
        if winners[0] == 0:
            assert await restarted.delete("run/a", idempotency_key="contended") is True
        else:
            assert await restarted.cleanup_run("run", idempotency_key="contended") == results[1]
