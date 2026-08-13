"""ZER-49: nothing may lose a concurrent append to the interrupt index.

Three passes closed this, each removing one caller of the read-modify-write
shape rather than making the write itself cleverer:

* **F-09** made ``_rewrite_list`` atomic -- one ``MULTI``/``EXEC`` instead of a
  ``DELETE`` plus a loop of ``RPUSH``, which any concurrent reader could observe
  half-applied. That closed the torn *read*, and explicitly not the lost append.
* **F-10** took the rewrite off the read path: ``list_requests`` filters instead
  of self-healing, and ``sweep_expired`` maintains the index it invalidates.
* **F-11** (this pass) took it off the last write path. ``delete_request`` read
  the index, computed the remainder and wrote it back, so a ``save_request``
  landing in that window was overwritten -- a live pending interrupt gone from
  ``list_pending`` while its payload key survived. It now issues one
  ``LREM key 0 id`` against the live list, which removes exactly the id being
  deleted and cannot clobber an append it never read.

``_rewrite_list`` survives with no callers, deliberately: it is the audited
primitive for any future whole-index write, and the tests below keep its two
non-obvious properties (one transaction, no redis TTL) pinned so a future caller
inherits them. Its docstring records why routing a write path through it is the
wrong move.

The fake below models the two properties that make the races observable: a plain
command is a round-trip (it yields to the loop), and a ``MULTI``/``EXEC``
pipeline applies its whole buffer without yielding.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict
from typing import Any

import pytest

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

    def lrem(self, key: str, count: int, value: str) -> _FakePipeline:
        self._stack.append(("lrem", key, (count, value)))
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
            elif op == "lrem":
                self._redis.apply_lrem(key, int(args[0]), str(args[1]))
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

    def apply_lrem(self, key: str, count: int, value: str) -> int:
        """Redis ``LREM`` semantics, ``count`` honoured rather than ignored.

        ``count > 0`` removes that many occurrences head-to-tail, ``count < 0``
        tail-to-head, ``count == 0`` removes every one. Modelling ``count`` for
        real is the point: a fake that always removed all occurrences could not
        tell a correct ``LREM key 0 id`` from a ``count=1`` that leaves a
        duplicate behind, so the store's choice of count would be untested.
        """
        items = self.lists.get(key)
        if items is None:
            return 0
        indexes = [i for i, item in enumerate(items) if item == value]
        if count > 0:
            indexes = indexes[:count]
        elif count < 0:
            indexes = indexes[count:]
        doomed = set(indexes)
        kept = [item for i, item in enumerate(items) if i not in doomed]
        if kept:
            self.lists[key] = kept
        else:  # redis drops a list key when its last element goes
            self.lists.pop(key, None)
        return len(doomed)

    async def lrem(self, key: str, count: int, value: str) -> int:
        await asyncio.sleep(0)
        return self.apply_lrem(key, count, value)

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


def _seed_live(fake: _FakeRedis, store: RedisInterruptStore, interrupt_ids: list[str]) -> None:
    """Index every id and give each one a live payload -- a consistent start state."""
    fake.lists[KEY] = list(interrupt_ids)
    for interrupt_id in interrupt_ids:
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


async def test_delete_request_updates_the_index_without_a_torn_read() -> None:
    """Renamed by ZER-49 F-11; every assertion below is unchanged from F-09.

    The old name, ``..._still_rewrites_the_index_atomically``, named a
    *mechanism*. The assertions never did: they say the index ends up as the
    remainder and that no concurrent reader observes an intermediate state.
    Both are properties of the outcome, and ``LREM key 0 id`` satisfies them the
    same way the ``MULTI``/``EXEC`` rewrite did -- one command, applied without
    yielding, leaving ``["i-a", "i-c"]``.

    So this is not a pin being weakened to let a change through. The mechanism
    moved out of the name and into the docstring; the guarantees kept running
    verbatim. What changed is the guarantee this test *cannot* express -- that a
    concurrent append survives -- which is why
    ``test_a_save_landing_during_a_delete_is_not_lost`` was added rather than
    this one relaxed.

    The TTL assertion is new, and it is the reason F-09's no-TTL decision does
    not lapse when the delete path stops going through ``_rewrite_list``: the
    property is now pinned on the path that actually runs.
    """
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
    assert fake.expires == {}, "the delete path must not put a TTL on the index"


async def test_delete_request_removes_every_occurrence_of_the_id() -> None:
    """Why the count is ``0`` and not ``1``.

    The rewrite this replaced computed ``[c for c in ids if c != interrupt_id]``
    -- a filter, which already dropped *every* occurrence. ``LREM key 0 id`` is
    therefore the count that preserves the existing contract; ``count=1`` would
    be a behaviour regression smuggled in by a bug fix, leaving a duplicate id
    pointing at a payload key that was just deleted.

    Duplicates are reachable, not hypothetical: ``save_request`` decides whether
    to append by reading the index first, so two concurrent first-saves of one id
    can both see it missing and both ``RPUSH``. That check-then-push is a
    *duplicate-append* hazard and out of scope here -- it is cited as the reason
    removal must be idempotent by identity, not fixed.
    """
    fake = _FakeRedis()
    fake.lists[KEY] = ["i-a", "i-dup", "i-b", "i-dup"]
    store = RedisInterruptStore(redis_url="redis://unused", redis_client=fake)

    await store.delete_request(RUN_ID, "i-dup")

    assert fake.lists[KEY] == ["i-a", "i-b"], "a deleted id must not survive as a duplicate"


@pytest.mark.parametrize("save_first", [True, False])
async def test_a_save_landing_during_a_delete_is_not_lost(save_first: bool) -> None:
    """ZER-49 F-11 negative control: the last lost-append site in this store.

    ``delete_request`` used to ``LRANGE`` the index, compute the remainder, and
    write it back. A ``save_request`` that appended between the read and the
    write was overwritten: the payload key survives, so the interrupt is live and
    pending, but its id is gone from the index and therefore from
    ``list_pending`` and ``get_latest_pending``. Nobody is ever asked for that
    approval.

    Both arrival orders are exercised because only one of them lands the append
    inside the old read-to-write window -- with ``save_first`` the writer's
    ``RPUSH`` resolves one scheduler step before the rewrite's ``EXEC``, and the
    rewrite wins. Against ``LREM`` neither order can lose it, because ``LREM``
    never reads the list into the process at all.
    """
    fake = _FakeRedis()
    store = RedisInterruptStore(redis_url="redis://unused", redis_client=fake)
    _seed_live(fake, store, ["i-a", "i-doomed"])
    arriving = _request("i-new")

    saving = store.save_request(arriving)
    deleting = store.delete_request(RUN_ID, "i-doomed")
    await asyncio.gather(*((saving, deleting) if save_first else (deleting, saving)))

    # Oracle: both operations really ran, so the interleaving is adversarial
    # rather than one of them silently no-opping.
    assert store._request_key(RUN_ID, "i-new") in fake.strings, "the save did not run"
    assert store._request_key(RUN_ID, "i-doomed") not in fake.strings, "the delete did not run"

    assert "i-doomed" not in fake.lists[KEY]
    assert "i-new" in fake.lists[KEY], "a concurrent save was overwritten by the delete"
    listed = [req.interrupt_id for req in await store.list_requests(RUN_ID)]
    assert listed == ["i-a", "i-new"], f"a live pending interrupt is unreachable: {listed}"
