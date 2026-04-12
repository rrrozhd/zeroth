"""Tests for ArtifactStore Protocol and Redis/Filesystem implementations."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zeroth.core.artifacts.errors import (
    ArtifactNotFoundError,
    ArtifactStorageError,
    ArtifactTTLError,
)
from zeroth.core.artifacts.models import ArtifactReference
from zeroth.core.artifacts.store import (
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
    def mock_redis(self) -> AsyncMock:
        """Create a mock Redis async client."""
        client = AsyncMock()
        # Pipeline mock
        pipeline = AsyncMock()
        pipeline.__aenter__ = AsyncMock(return_value=pipeline)
        pipeline.__aexit__ = AsyncMock(return_value=False)
        client.pipeline.return_value = pipeline
        # Default exists returns 0 (not found)
        client.exists.return_value = 0
        return client

    @pytest.fixture()
    def store(self, mock_redis: AsyncMock) -> RedisArtifactStore:
        """Create a RedisArtifactStore with mocked client."""
        s = RedisArtifactStore(
            redis_url="redis://localhost:6379/0",
            prefix="zeroth:artifact",
            default_ttl=3600,
            max_size=104857600,
        )
        s._client = mock_redis
        return s

    @pytest.mark.asyncio()
    async def test_store_with_ttl(self, store: RedisArtifactStore, mock_redis: AsyncMock) -> None:
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
    async def test_store_without_ttl(self, store: RedisArtifactStore, mock_redis: AsyncMock) -> None:
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
    async def test_retrieve_existing(self, store: RedisArtifactStore, mock_redis: AsyncMock) -> None:
        """retrieve() returns bytes for existing key."""
        mock_redis.get.return_value = b"file-contents"

        result = await store.retrieve("run1/node1/abc123")

        assert result == b"file-contents"
        mock_redis.get.assert_called_once_with("zeroth:artifact:run1/node1/abc123")

    @pytest.mark.asyncio()
    async def test_retrieve_missing(self, store: RedisArtifactStore, mock_redis: AsyncMock) -> None:
        """retrieve() raises ArtifactNotFoundError for missing key."""
        mock_redis.get.return_value = None

        with pytest.raises(ArtifactNotFoundError):
            await store.retrieve("run1/node1/missing")

    @pytest.mark.asyncio()
    async def test_delete_existing(self, store: RedisArtifactStore, mock_redis: AsyncMock) -> None:
        """delete() returns True for existing key."""
        pipeline = mock_redis.pipeline.return_value
        pipeline.execute.return_value = [1, 1]

        result = await store.delete("run1/node1/abc123")

        assert result is True

    @pytest.mark.asyncio()
    async def test_delete_missing(self, store: RedisArtifactStore, mock_redis: AsyncMock) -> None:
        """delete() returns False for missing key."""
        pipeline = mock_redis.pipeline.return_value
        pipeline.execute.return_value = [0, 0]

        result = await store.delete("run1/node1/missing")

        assert result is False

    @pytest.mark.asyncio()
    async def test_refresh_ttl_existing(
        self, store: RedisArtifactStore, mock_redis: AsyncMock
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
        self, store: RedisArtifactStore, mock_redis: AsyncMock
    ) -> None:
        """refresh_ttl() raises ArtifactTTLError when key does not exist."""
        mock_redis.exists.return_value = 0

        with pytest.raises(ArtifactTTLError):
            await store.refresh_ttl("run1/node1/missing", 600)

    @pytest.mark.asyncio()
    async def test_exists_true(self, store: RedisArtifactStore, mock_redis: AsyncMock) -> None:
        """exists() returns True based on redis exists command."""
        mock_redis.exists.return_value = 1

        result = await store.exists("run1/node1/abc123")

        assert result is True

    @pytest.mark.asyncio()
    async def test_exists_false(self, store: RedisArtifactStore, mock_redis: AsyncMock) -> None:
        """exists() returns False when key missing."""
        mock_redis.exists.return_value = 0

        result = await store.exists("run1/node1/missing")

        assert result is False

    @pytest.mark.asyncio()
    async def test_cleanup_run(self, store: RedisArtifactStore, mock_redis: AsyncMock) -> None:
        """cleanup_run() uses scan_iter with prefix pattern, deletes matching keys."""
        mock_redis.scan_iter.return_value = self._async_iter(
            [b"zeroth:artifact:run1/a", b"zeroth:artifact:run1/b"]
        )
        mock_redis.delete.return_value = 1

        count = await store.cleanup_run("run1")

        assert count == 2
        mock_redis.scan_iter.assert_called_once()
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
    async def test_store_rejects_oversized_payload(
        self, store: FilesystemArtifactStore
    ) -> None:
        """store() rejects payload exceeding max_artifact_size_bytes."""
        store._max_size = 10
        with pytest.raises(ArtifactStorageError, match="exceeds maximum"):
            await store.store("run1/node1/abc", b"x" * 11, "text/plain")

    @pytest.mark.asyncio()
    async def test_retrieve_existing(
        self, store: FilesystemArtifactStore, tmp_path: Path
    ) -> None:
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
    async def test_retrieve_expired(
        self, store: FilesystemArtifactStore, tmp_path: Path
    ) -> None:
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
    async def test_delete_existing(
        self, store: FilesystemArtifactStore, tmp_path: Path
    ) -> None:
        """delete() removes both file and sidecar, returns True."""
        await store.store("run1/node1/abc123", b"data", "text/plain")

        result = await store.delete("run1/node1/abc123")

        assert result is True
        assert not (tmp_path / "run1" / "node1" / "abc123").exists()
        assert not (tmp_path / "run1" / "node1" / "abc123.meta.json").exists()

    @pytest.mark.asyncio()
    async def test_delete_missing(self, store: FilesystemArtifactStore) -> None:
        """delete() returns False for missing file."""
        result = await store.delete("run1/node1/nonexistent")

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
    async def test_exists_true(
        self, store: FilesystemArtifactStore, tmp_path: Path
    ) -> None:
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
    async def test_cleanup_run(
        self, store: FilesystemArtifactStore, tmp_path: Path
    ) -> None:
        """cleanup_run() removes entire {base_dir}/{run_id}/ directory tree."""
        await store.store("myrun/node1/file1", b"data1", "text/plain")
        await store.store("myrun/node2/file2", b"data2", "text/plain")
        await store.store("otherrun/node1/file3", b"data3", "text/plain")

        count = await store.cleanup_run("myrun")

        assert count > 0
        assert not (tmp_path / "myrun").exists()
        # Other runs should be untouched
        assert (tmp_path / "otherrun").exists()

    @pytest.mark.asyncio()
    async def test_path_traversal_rejected(self, store: FilesystemArtifactStore) -> None:
        """Key containing '..' raises ArtifactStorageError (path traversal prevention)."""
        with pytest.raises(ArtifactStorageError, match="path traversal"):
            await store.store("run1/../../../etc/passwd", b"evil", "text/plain")
