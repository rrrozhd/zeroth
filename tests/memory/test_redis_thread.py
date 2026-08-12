"""Unit tests for RedisThreadMemoryConnector."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from zeroth.integrations.memory.governed.connector import MemoryConnector
from zeroth.integrations.memory.governed.models import MemoryEntry, MemoryScope
from zeroth.integrations.memory.redis_thread import RedisThreadMemoryConnector

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
        mock_redis.zrevrange.assert_awaited_once_with("zeroth:mem:thread:thread:t-1:messages", 0, 0)

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

        mock_redis.delete.assert_awaited_once_with("zeroth:mem:thread:thread:t-1:messages")

    @pytest.mark.asyncio
    async def test_delete_nonexistent_raises_key_error(self) -> None:
        connector, mock_redis = _make_connector()
        mock_redis.delete.return_value = 0

        with pytest.raises(KeyError):
            await connector.delete("nonexistent", MemoryScope.THREAD, target="t-1")


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def _thread_entry(content: str, key: str = "messages") -> MemoryEntry:
    """Build a thread entry whose value carries *content*."""
    return MemoryEntry(
        key=key,
        value={"role": "user", "content": content},
        scope=MemoryScope.THREAD,
        scope_target="t-1",
    )


def _install_sorted_sets(mock_redis: AsyncMock, sets: dict[bytes, list[tuple[float, str]]]) -> None:
    """Wire ``scan_iter``/``zrevrange`` onto *mock_redis* over in-memory sets.

    ``sets`` maps a Redis key to ``(score, member)`` pairs. ``scan_iter``
    yields keys in the given order -- the point being that it is *not* score
    order, so a connector that trusts scan order is caught.
    """

    async def fake_scan_iter(match: str = "*"):
        for redis_key in sets:
            yield redis_key

    async def fake_zrevrange(
        redis_key: bytes, start: int, stop: int, withscores: bool = False
    ) -> list:
        bucket = sorted(sets[redis_key], key=lambda pair: pair[0], reverse=True)
        window = bucket[start:] if stop == -1 else bucket[start : stop + 1]
        if withscores:
            return [(member.encode(), score) for score, member in window]
        return [member.encode() for _, member in window]

    mock_redis.scan_iter = fake_scan_iter
    mock_redis.zrevrange = AsyncMock(side_effect=fake_zrevrange)


class TestSearch:
    @pytest.mark.asyncio
    async def test_search_by_text(self) -> None:
        connector, mock_redis = _make_connector()

        entry_hello = _thread_entry("hello world")
        entry_bye = _thread_entry("goodbye")
        _install_sorted_sets(
            mock_redis,
            {
                b"zeroth:mem:thread:thread:t-1:messages": [
                    (2.0, entry_hello.model_dump_json()),
                    (1.0, entry_bye.model_dump_json()),
                ]
            },
        )

        results = await connector.search({"text": "hello"}, MemoryScope.THREAD, target="t-1")

        assert len(results) == 1
        assert results[0].value["content"] == "hello world"

    @pytest.mark.asyncio
    async def test_search_with_limit(self) -> None:
        connector, mock_redis = _make_connector()

        entries = [_thread_entry(f"msg-{i}") for i in range(10)]
        _install_sorted_sets(
            mock_redis,
            {
                b"zeroth:mem:thread:thread:t-1:messages": [
                    (float(i), e.model_dump_json()) for i, e in enumerate(entries)
                ]
            },
        )

        results = await connector.search({"limit": 5}, MemoryScope.THREAD, target="t-1")

        assert [r.value["content"] for r in results] == [f"msg-{i}" for i in (9, 8, 7, 6, 5)]

    @pytest.mark.asyncio
    async def test_search_merges_across_keys_by_recency_not_scan_order(self) -> None:
        """A07-21: the newest entries win, whichever key ``scan_iter`` reached first.

        The old code appended each key's ``zrevrange`` run in scan order and
        sliced, so with ``limit=3`` it returned the three oldest entries --
        they merely happened to live in the key Redis scanned first.
        """
        connector, mock_redis = _make_connector()

        old = [_thread_entry(f"old-{i}", key="a") for i in range(3)]
        new = [_thread_entry(f"new-{i}", key="b") for i in range(3)]
        _install_sorted_sets(
            mock_redis,
            {
                # Scanned first, but every score here is lower.
                b"zeroth:mem:thread:thread:t-1:a": [
                    (10.0 + i, e.model_dump_json()) for i, e in enumerate(old)
                ],
                b"zeroth:mem:thread:thread:t-1:b": [
                    (100.0 + i, e.model_dump_json()) for i, e in enumerate(new)
                ],
            },
        )

        results = await connector.search({"limit": 3}, MemoryScope.THREAD, target="t-1")

        assert [r.value["content"] for r in results] == ["new-2", "new-1", "new-0"]

    @pytest.mark.asyncio
    async def test_search_with_zero_limit_reads_nothing(self) -> None:
        """A07-21: ``limit=0`` must mean zero results, not ``zrevrange(key, 0, -1)``."""
        connector, mock_redis = _make_connector()

        entries = [_thread_entry(f"msg-{i}") for i in range(3)]
        _install_sorted_sets(
            mock_redis,
            {
                b"zeroth:mem:thread:thread:t-1:messages": [
                    (float(i), e.model_dump_json()) for i, e in enumerate(entries)
                ]
            },
        )

        results = await connector.search({"limit": 0}, MemoryScope.THREAD, target="t-1")

        assert results == []
        mock_redis.zrevrange.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_search_rejects_a_negative_limit(self) -> None:
        connector, mock_redis = _make_connector()
        _install_sorted_sets(mock_redis, {})

        with pytest.raises(ValueError, match="non-negative integer"):
            await connector.search({"limit": -1}, MemoryScope.THREAD, target="t-1")


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
        """Append-and-read conversation history against a real Redis sorted set.

        Connection comes from ``ZEROTH_TEST_REDIS_URL`` (default
        ``redis://localhost:6379/0``); skips if Redis is unreachable.
        """
        import asyncio
        import os

        import redis.asyncio as aioredis

        url = os.environ.get("ZEROTH_TEST_REDIS_URL", "redis://localhost:6379/0")
        client = aioredis.from_url(url)
        try:
            await client.ping()
        except Exception as exc:  # noqa: BLE001
            await client.aclose()
            pytest.skip(f"Redis not reachable at {url}: {exc}")

        connector = RedisThreadMemoryConnector(client, key_prefix="zeroth:test:thread")
        try:
            await connector.write(
                "messages", {"role": "user", "content": "hello"}, MemoryScope.THREAD, target="t-1"
            )
            # Distinct sorted-set scores so "most recent" is unambiguous.
            await asyncio.sleep(0.01)
            await connector.write(
                "messages",
                {"role": "assistant", "content": "hi there"},
                MemoryScope.THREAD,
                target="t-1",
            )

            # read returns the most recent entry.
            latest = await connector.read("messages", MemoryScope.THREAD, target="t-1")
            assert latest is not None
            assert latest.value == {"role": "assistant", "content": "hi there"}

            # search preserves the full history and supports text filtering.
            all_msgs = await connector.search({}, MemoryScope.THREAD, target="t-1")
            assert len(all_msgs) >= 2
            filtered = await connector.search({"text": "hello"}, MemoryScope.THREAD, target="t-1")
            assert any(m.value.get("content") == "hello" for m in filtered)

            # Delete drops the whole history; a second delete raises KeyError.
            await connector.delete("messages", MemoryScope.THREAD, target="t-1")
            assert await connector.read("messages", MemoryScope.THREAD, target="t-1") is None
            with pytest.raises(KeyError):
                await connector.delete("messages", MemoryScope.THREAD, target="t-1")
        finally:
            async for stale in client.scan_iter(match="zeroth:test:thread:*"):
                await client.delete(stale)
            await client.aclose()
