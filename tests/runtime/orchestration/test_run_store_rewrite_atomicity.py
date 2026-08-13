"""ZER-49 A06-13: ``RedisRunStore._rewrite_list`` must not tear the list.

``_rewrite_list`` used to issue ``DELETE`` and then a *loop* of ``RPUSH`` as
separate round-trips, so any concurrent reader between them saw an empty or
half-rebuilt index. It is called from read paths (``list_run_ids``,
``delete``, and ``interrupts.get_requests``), so an ordinary read could wipe
another reader's view of the thread's runs for the duration of the rewrite.

The fake below models the two properties that matter: a plain command is a
round-trip (it yields to the loop), and a ``MULTI``/``EXEC`` pipeline applies
its whole buffer without yielding.
"""

from __future__ import annotations

import asyncio
from typing import Any

from zeroth.runtime.orchestration.run_store import RedisRunStore

KEY = "governai:run:thread:t-1:runs"


class _FakePipeline:
    """Buffers commands and applies them in one non-yielding step."""

    def __init__(self, redis: _FakeRedis) -> None:
        self._redis = redis
        self._stack: list[tuple[str, str, tuple[Any, ...]]] = []

    async def __aenter__(self) -> _FakePipeline:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def delete(self, key: str) -> _FakePipeline:
        self._stack.append(("delete", key, ()))
        return self

    def rpush(self, key: str, *values: Any) -> _FakePipeline:
        self._stack.append(("rpush", key, values))
        return self

    def expire(self, key: str, ttl: int) -> _FakePipeline:
        self._stack.append(("expire", key, (ttl,)))
        return self

    async def execute(self) -> list[Any]:
        # The round-trip happens BEFORE anything is applied; the buffer then
        # applies atomically, with no await in between.
        await asyncio.sleep(0)
        for op, key, args in self._stack:
            if op == "delete":
                self._redis.lists.pop(key, None)
            elif op == "rpush":
                self._redis.lists.setdefault(key, []).extend(str(v) for v in args)
            elif op == "expire":
                self._redis.expires[key] = int(args[0])
        self._stack.clear()
        return []


class _FakeRedis:
    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}
        self.expires: dict[str, int] = {}
        self.pipelines_opened = 0

    async def delete(self, key: str) -> None:
        await asyncio.sleep(0)
        self.lists.pop(key, None)

    async def rpush(self, key: str, *values: Any) -> None:
        await asyncio.sleep(0)
        self.lists.setdefault(key, []).extend(str(v) for v in values)

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        await asyncio.sleep(0)
        return list(self.lists.get(key, []))

    async def expire(self, key: str, ttl: int) -> None:
        await asyncio.sleep(0)
        self.expires[key] = int(ttl)

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        assert transaction, "the rewrite must ask for a transactional pipeline"
        self.pipelines_opened += 1
        return _FakePipeline(self)


async def _sample_until(fake: _FakeRedis, key: str, stop: asyncio.Event, out: list[list[str]]):
    while not stop.is_set():
        out.append(list(fake.lists.get(key, [])))
        await asyncio.sleep(0)


async def test_a_concurrent_reader_never_sees_a_torn_list() -> None:
    fake = _FakeRedis()
    old = ["run-a", "run-b", "run-stale", "run-c"]
    new = ["run-a", "run-b", "run-c"]
    fake.lists[KEY] = list(old)
    store = RedisRunStore(redis_url="redis://unused", redis_client=fake)

    stop = asyncio.Event()
    samples: list[list[str]] = []
    sampler = asyncio.create_task(_sample_until(fake, KEY, stop, samples))

    await store._rewrite_list(KEY, new)

    stop.set()
    await sampler

    assert fake.lists[KEY] == new
    torn = [s for s in samples if s not in (old, new)]
    assert not torn, f"reader observed a torn list mid-rewrite: {torn}"


async def test_the_rewrite_uses_one_transactional_pipeline() -> None:
    fake = _FakeRedis()
    fake.lists[KEY] = ["run-a"]
    store = RedisRunStore(redis_url="redis://unused", redis_client=fake, ttl_seconds=60)

    await store._rewrite_list(KEY, ["run-a", "run-b"])

    assert fake.pipelines_opened == 1
    assert fake.lists[KEY] == ["run-a", "run-b"]
    assert fake.expires[KEY] == 60


async def test_an_empty_rewrite_deletes_the_key_without_a_ttl() -> None:
    fake = _FakeRedis()
    fake.lists[KEY] = ["run-a"]
    store = RedisRunStore(redis_url="redis://unused", redis_client=fake, ttl_seconds=60)

    await store._rewrite_list(KEY, [])

    assert KEY not in fake.lists
    assert KEY not in fake.expires
