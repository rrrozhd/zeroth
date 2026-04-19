"""Unit tests for RedisThreadMemoryConnector."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from governai.memory.connector import MemoryConnector
from governai.memory.models import MemoryEntry, MemoryScope

from zeroth.core.memory.redis_thread import RedisThreadMemoryConnector

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_connector(
    mock_redis: AsyncMock | None = None,
) -> tuple[RedisThreadMemoryConnector, AsyncMock]:
    """Create a connector with a mocked Redis client."""
    if mock_redis is None:
        mock_redis = AsyncMock()
    connector = RedisThreadMemoryConnector(mock_redis, key_prefix="zeroth:mem:thread")
    return connector, mock_redis


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_isinstance_check(self) -> None:
        connector, _ = _make_connector()
        assert isinstance(connector, MemoryConnector)

    def test_connector_type(self) -> None:
        connector, _ = _make_connector()
        assert connector.connector_type == "redis_thread"


# ---------------------------------------------------------------------------
# read (returns most recent entry from sorted set)
# ---------------------------------------------------------------------------


class TestRead:
    @pytest.mark.asyncio
    async def test_read_returns_latest_entry(self) -> None:
        connector, mock_redis = _make_connector()
        entry = MemoryEntry(
            key="messages",
            value={"role": "user", "content": "hello"},
            scope=MemoryScope.THREAD,
            scope_target="t-1",
        )
        # zrevrange returns list of members (most recent first)
        mock_redis.zrevrange.return_value = [entry.model_dump_json().encode()]

        result = await connector.read("messages", MemoryScope.THREAD, target="t-1")

        assert result is not None
        assert result.key == "messages"
        assert result.value == {"role": "user", "content": "hello"}
        mock_redis.zrevrange.assert_awaited_once_with(
            "zeroth:mem:thread:thread:t-1:messages", 0, 0
        )

    @pytest.mark.asyncio
    async def test_read_empty_set_returns_none(self) -> None:
        connector, mock_redis = _make_connector()
        mock_redis.zrevrange.return_value = []

        result = await connector.read("messages", MemoryScope.THREAD, target="t-1")

        assert result is None


# ---------------------------------------------------------------------------
# write (appends to sorted set with timestamp score)
# ---------------------------------------------------------------------------


class TestWrite:
    @pytest.mark.asyncio
    async def test_write_appends_to_sorted_set(self) -> None:
        connector, mock_redis = _make_connector()

        await connector.write(
            "messages",
            {"role": "user", "content": "hello"},
            MemoryScope.THREAD,
            target="t-1",
        )

        mock_redis.zadd.assert_awaited_once()
        call_args = mock_redis.zadd.call_args
        redis_key = call_args[0][0]
        mapping = call_args[0][1]

        assert redis_key == "zeroth:mem:thread:thread:t-1:messages"
        # Mapping is {json_str: score}
        assert len(mapping) == 1
        json_str = next(iter(mapping))
        parsed = json.loads(json_str)
        assert parsed["key"] == "messages"
        assert parsed["value"] == {"role": "user", "content": "hello"}
        assert parsed["scope"] == "thread"

    @pytest.mark.asyncio
    async def test_multiple_writes_accumulate(self) -> None:
        """Multiple writes to same key should call zadd multiple times (append)."""
        connector, mock_redis = _make_connector()

        await connector.write(
            "messages", {"role": "user", "content": "hello"}, MemoryScope.THREAD, target="t-1"
        )
        await connector.write(
            "messages", {"role": "assistant", "content": "hi"}, MemoryScope.THREAD, target="t-1"
        )

        assert mock_redis.zadd.await_count == 2


# ---------------------------------------------------------------------------
# delete (removes entire sorted set)
# ---------------------------------------------------------------------------


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_removes_key(self) -> None:
        connector, mock_redis = _make_connector()
        mock_redis.delete.return_value = 1

        await connector.delete("messages", MemoryScope.THREAD, target="t-1")

        mock_redis.delete.assert_awaited_once_with(
            "zeroth:mem:thread:thread:t-1:messages"
        )

    @pytest.mark.asyncio
    async def test_delete_nonexistent_raises_key_error(self) -> None:
        connector, mock_redis = _make_connector()
        mock_redis.delete.return_value = 0

        with pytest.raises(KeyError):
            await connector.delete("nonexistent", MemoryScope.THREAD, target="t-1")


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


class TestSearch:
    @pytest.mark.asyncio
    async def test_search_by_text(self) -> None:
        connector, mock_redis = _make_connector()

        entry_hello = MemoryEntry(
            key="messages",
            value={"role": "user", "content": "hello world"},
            scope=MemoryScope.THREAD,
            scope_target="t-1",
        )
        entry_bye = MemoryEntry(
            key="messages",
            value={"role": "user", "content": "goodbye"},
            scope=MemoryScope.THREAD,
            scope_target="t-1",
        )

        async def fake_scan_iter(match: str = "*"):  # noqa: ANN001
            yield b"zeroth:mem:thread:thread:t-1:messages"

        mock_redis.scan_iter = fake_scan_iter
        mock_redis.zrevrange = AsyncMock(
            return_value=[
                entry_hello.model_dump_json().encode(),
                entry_bye.model_dump_json().encode(),
            ]
        )

        results = await connector.search({"text": "hello"}, MemoryScope.THREAD, target="t-1")

        assert len(results) == 1
        assert results[0].value["content"] == "hello world"

    @pytest.mark.asyncio
    async def test_search_with_limit(self) -> None:
        connector, mock_redis = _make_connector()

        entries = [
            MemoryEntry(
                key="messages",
                value={"role": "user", "content": f"msg-{i}"},
                scope=MemoryScope.THREAD,
                scope_target="t-1",
            )
            for i in range(10)
        ]

        async def fake_scan_iter(match: str = "*"):  # noqa: ANN001
            yield b"zeroth:mem:thread:thread:t-1:messages"

        mock_redis.scan_iter = fake_scan_iter
        mock_redis.zrevrange = AsyncMock(
            return_value=[e.model_dump_json().encode() for e in entries[:5]]
        )

        results = await connector.search({"limit": 5}, MemoryScope.THREAD, target="t-1")

        assert len(results) <= 5


# ---------------------------------------------------------------------------
# Scope isolation
# ---------------------------------------------------------------------------


class TestScopeIsolation:
    @pytest.mark.asyncio
    async def test_different_threads_produce_different_keys(self) -> None:
        connector, mock_redis = _make_connector()
        mock_redis.zrevrange.return_value = []

        await connector.read("messages", MemoryScope.THREAD, target="t-1")
        await connector.read("messages", MemoryScope.THREAD, target="t-2")

        calls = mock_redis.zrevrange.call_args_list
        assert calls[0][0][0] == "zeroth:mem:thread:thread:t-1:messages"
        assert calls[1][0][0] == "zeroth:mem:thread:thread:t-2:messages"


# ---------------------------------------------------------------------------
# Key format
# ---------------------------------------------------------------------------


class TestKeyFormat:
    def test_key_format_structure(self) -> None:
        connector, _ = _make_connector()
        key = connector._key("messages", MemoryScope.THREAD, "t-1")
        assert key == "zeroth:mem:thread:thread:t-1:messages"

    def test_key_prefix_distinct_from_kv(self) -> None:
        """Thread prefix must differ from KV prefix to prevent data collision."""
        connector, _ = _make_connector()
        assert "zeroth:mem:thread" in connector._prefix
        assert connector._prefix != "zeroth:mem:kv"


# ---------------------------------------------------------------------------
# Integration test stub (skipped without live marker)
# ---------------------------------------------------------------------------


@pytest.mark.live
class TestRedisThreadIntegration:
    """Integration tests that require a real Redis instance.

    Run with: uv run pytest -m live tests/memory/test_redis_thread.py
    """

    @pytest.mark.asyncio
    async def test_conversation_roundtrip_live(self) -> None:
        """Requires a running Redis. Skipped by default."""
        pytest.skip("Requires live Redis -- run with -m live")
