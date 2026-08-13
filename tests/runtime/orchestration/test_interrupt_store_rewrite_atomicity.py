"""ZER-49 F-09: ``RedisInterruptStore._rewrite_list`` must not tear the index.

This is the sibling of ``test_run_store_rewrite_atomicity``. ``RedisRunStore``
was fixed; ``RedisInterruptStore`` is a separate class that never inherited the
fix and carried the same ``DELETE`` + loop-of-``RPUSH`` shape.

It matters more here than the shape alone suggests, because the rewrite fires
from a *read* path: ``list_requests`` self-heals the index whenever one of the
indexed requests has expired out from under it. So an ordinary listing could
blank another reader's view of a run's pending interrupts.

The fake below models the two properties that make the tear observable: a plain
command is a round-trip (it yields to the loop), and a ``MULTI``/``EXEC``
pipeline applies its whole buffer without yielding.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict
from typing import Any

from zeroth.runtime.orchestration.interrupts import InterruptRequest, RedisInterruptStore

RUN_ID = "run-1"
KEY = "governai:interrupt:run:run-1:requests"


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
        assert values, "RPUSH with no values is a redis error; an empty rewrite is delete-only"
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
                self._redis.strings.pop(key, None)
            elif op == "rpush":
                self._redis.lists.setdefault(key, []).extend(str(v) for v in args)
            elif op == "expire":
                self._redis.expires[key] = int(args[0])
        self._stack.clear()
        return []


class _FakeRedis:
    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}
        self.strings: dict[str, str] = {}
        self.expires: dict[str, int] = {}
        self.pipelines_opened = 0

    async def get(self, key: str) -> str | None:
        await asyncio.sleep(0)
        return self.strings.get(key)

    async def set(self, key: str, value: str) -> None:
        await asyncio.sleep(0)
        self.strings[key] = value

    async def delete(self, key: str) -> None:
        await asyncio.sleep(0)
        self.lists.pop(key, None)
        self.strings.pop(key, None)

    async def rpush(self, key: str, *values: Any) -> None:
        await asyncio.sleep(0)
        assert values, "RPUSH with no values is a redis error"
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


def _request(interrupt_id: str) -> InterruptRequest:
    now_ts = int(time.time())
    return InterruptRequest(
        interrupt_id=interrupt_id,
        run_id=RUN_ID,
        step_name="review",
        message="needs a human",
        created_at=now_ts,
        expires_at=now_ts + 600,
    )


def _seed(fake: _FakeRedis, store: RedisInterruptStore, interrupt_ids: list[str]) -> None:
    """Put ids in the index; give every id but the last a live payload key."""
    fake.lists[KEY] = list(interrupt_ids)
    for interrupt_id in interrupt_ids[:-1]:
        key = store._request_key(RUN_ID, interrupt_id)
        fake.strings[key] = json.dumps(asdict(_request(interrupt_id)))


async def _sample_until(
    fake: _FakeRedis, key: str, stop: asyncio.Event, out: list[list[str]]
) -> None:
    while not stop.is_set():
        out.append(list(fake.lists.get(key, [])))
        await asyncio.sleep(0)


async def test_a_concurrent_reader_never_sees_a_torn_interrupt_index() -> None:
    fake = _FakeRedis()
    old = ["i-a", "i-b", "i-stale", "i-c"]
    new = ["i-a", "i-b", "i-c"]
    fake.lists[KEY] = list(old)
    store = RedisInterruptStore(redis_url="redis://unused", redis_client=fake)

    stop = asyncio.Event()
    samples: list[list[str]] = []
    sampler = asyncio.create_task(_sample_until(fake, KEY, stop, samples))

    await store._rewrite_list(KEY, new)

    stop.set()
    await sampler

    assert fake.lists[KEY] == new
    torn = [s for s in samples if s not in (old, new)]
    assert not torn, f"reader observed a torn interrupt index mid-rewrite: {torn}"


async def test_the_read_path_no_longer_mutates_the_index_at_all() -> None:
    """Re-targeted by ZER-49 F-10, which deleted the read-path self-heal.

    This test used to assert that ``list_requests``' self-heal rewrite did not
    *tear* the index. The MULTI/EXEC above still guarantees that for every
    rewrite, but the read path no longer performs one: healing the index on a
    read was a read-modify-write that overwrote concurrent appends, and expiry
    (``sweep_expired``) now owns index maintenance. Tear-freedom on this path is
    vacuous once the mutation is gone, so the stronger property is pinned
    instead -- a listing filters, and writes nothing.

    ``_seed`` gives the last id no payload key, which is exactly the shape that
    used to trigger the rewrite.
    """
    fake = _FakeRedis()
    old = ["i-a", "i-b", "i-expired"]
    store = RedisInterruptStore(redis_url="redis://unused", redis_client=fake)
    _seed(fake, store, old)

    stop = asyncio.Event()
    samples: list[list[str]] = []
    sampler = asyncio.create_task(_sample_until(fake, KEY, stop, samples))

    listed = await store.list_requests(RUN_ID)

    stop.set()
    await sampler

    assert [req.interrupt_id for req in listed] == ["i-a", "i-b"]
    assert fake.pipelines_opened == 0, "a read opened a write transaction"
    assert fake.lists[KEY] == old, "the read path mutated the index"
    assert all(sample == old for sample in samples)


async def test_the_index_rewrite_uses_one_transactional_pipeline() -> None:
    fake = _FakeRedis()
    fake.lists[KEY] = ["i-a"]
    store = RedisInterruptStore(redis_url="redis://unused", redis_client=fake)

    await store._rewrite_list(KEY, ["i-a", "i-b"])

    assert fake.pipelines_opened == 1
    assert fake.lists[KEY] == ["i-a", "i-b"]


async def test_the_rewrite_applies_no_redis_ttl_because_this_store_has_none() -> None:
    """Deliberate divergence from ``RedisRunStore._rewrite_list``.

    ``RedisInterruptStore`` has no ``ttl_seconds`` -- its constructor is pinned
    in ``tests/contracts/fixtures/backend_surface_canonical.json`` and it expires
    requests at the application layer (``expires_at`` + ``sweep_expired``), never
    with a redis TTL. Expiring the index alone would drop live interrupts from
    ``list_pending`` while their payload keys survived forever.
    """
    fake = _FakeRedis()
    store = RedisInterruptStore(redis_url="redis://unused", redis_client=fake)

    assert not hasattr(store, "ttl_seconds")

    await store._rewrite_list(KEY, ["i-a"])

    assert fake.expires == {}


async def test_an_empty_rewrite_deletes_the_index_key() -> None:
    fake = _FakeRedis()
    fake.lists[KEY] = ["i-a"]
    store = RedisInterruptStore(redis_url="redis://unused", redis_client=fake)

    await store._rewrite_list(KEY, [])

    assert KEY not in fake.lists
    assert fake.expires == {}


async def test_delete_request_still_rewrites_the_index_atomically() -> None:
    fake = _FakeRedis()
    old = ["i-a", "i-b", "i-c"]
    remaining = ["i-a", "i-c"]
    store = RedisInterruptStore(redis_url="redis://unused", redis_client=fake)
    fake.lists[KEY] = list(old)

    stop = asyncio.Event()
    samples: list[list[str]] = []
    sampler = asyncio.create_task(_sample_until(fake, KEY, stop, samples))

    await store.delete_request(RUN_ID, "i-b")

    stop.set()
    await sampler

    assert fake.lists[KEY] == remaining
    torn = [s for s in samples if s not in (old, remaining)]
    assert not torn, f"a concurrent reader saw a torn index during a delete: {torn}"
