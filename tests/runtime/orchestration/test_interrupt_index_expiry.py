"""ZER-49: expiry owns the interrupt index; reads do not maintain it.

``RedisInterruptStore`` keeps two kinds of key per run::

    payload  governai:interrupt:run:{run_id}:request:{interrupt_id}
    index    governai:interrupt:run:{run_id}:requests

``sweep_expired`` SCANs ``{prefix}:run:*:request:*`` -- a pattern that matches
the payload keys and, because there is no colon after ``request``, never matches
the index key. So the sweeper deleted expired payloads and left their ids in the
index forever. Every reader inherited that dirt, which is why ``list_requests``
used to *rewrite the index on a read path*: a read-modify-write whose write
overwrote any ``save_request`` that landed in between, silently dropping a live
pending interrupt that a human was waiting on.

The tests below pin the corrected division of labour:

* the sweeper de-indexes what it deletes, and drops ids whose payload is gone;
* ``list_requests`` is a pure read -- it filters, it never writes.

The fake models the two properties that make the races observable: every command
is a round-trip (it yields to the loop), and ``LRANGE`` against a string key
raises ``WRONGTYPE`` the way redis does.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import time
from dataclasses import asdict
from typing import Any

import pytest

from zeroth.runtime.orchestration.interrupts import InterruptRequest, RedisInterruptStore

PREFIX = "governai:interrupt"


def _index_key(run_id: str) -> str:
    return f"{PREFIX}:run:{run_id}:requests"


def _payload_key(run_id: str, interrupt_id: str) -> str:
    return f"{PREFIX}:run:{run_id}:request:{interrupt_id}"


class _WrongTypeError(Exception):
    """Stand-in for redis ``WRONGTYPE`` -- a list command against a string key."""


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

    async def execute(self) -> list[Any]:
        await asyncio.sleep(0)
        for op, key, args in self._stack:
            self._redis.writes.append((op, key))
            if op == "delete":
                self._redis.lists.pop(key, None)
                self._redis.strings.pop(key, None)
            elif op == "rpush":
                self._redis.lists.setdefault(key, []).extend(str(v) for v in args)
        self._stack.clear()
        return []


class _FakeRedis:
    """Enough redis to drive the store, with every command a round-trip."""

    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        #: every mutating command, in order -- a read path must leave this empty.
        self.writes: list[tuple[str, str]] = []

    async def get(self, key: str) -> str | None:
        await asyncio.sleep(0)
        return self.strings.get(key)

    async def set(self, key: str, value: str) -> None:
        await asyncio.sleep(0)
        self.writes.append(("set", key))
        self.strings[key] = value

    async def delete(self, key: str) -> None:
        await asyncio.sleep(0)
        self.writes.append(("delete", key))
        self.strings.pop(key, None)
        self.lists.pop(key, None)

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        await asyncio.sleep(0)
        if key in self.strings:
            raise _WrongTypeError(f"WRONGTYPE: {key} holds a string, not a list")
        return list(self.lists.get(key, []))

    async def rpush(self, key: str, *values: Any) -> None:
        await asyncio.sleep(0)
        assert values, "RPUSH with no values is a redis error"
        self.writes.append(("rpush", key))
        self.lists.setdefault(key, []).extend(str(v) for v in values)

    async def lrem(self, key: str, count: int, value: str) -> int:
        await asyncio.sleep(0)
        if key in self.strings:
            raise _WrongTypeError(f"WRONGTYPE: {key} holds a string, not a list")
        self.writes.append(("lrem", key))
        items = self.lists.get(key)
        if items is None:
            return 0
        kept = [item for item in items if item != value]
        removed = len(items) - len(kept)
        if kept:
            self.lists[key] = kept
        else:  # redis drops a list key when its last element goes
            self.lists.pop(key, None)
        return removed

    async def scan(
        self, cursor: int = 0, match: str | None = None, count: int = 10
    ) -> tuple[int, list[str]]:
        await asyncio.sleep(0)
        keys = sorted(set(self.strings) | set(self.lists))
        if match is not None:
            keys = [key for key in keys if fnmatch.fnmatchcase(key, match)]
        page = keys[cursor : cursor + count]
        nxt = cursor + count
        return (nxt if nxt < len(keys) else 0), page

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        assert transaction, "the rewrite must ask for a transactional pipeline"
        return _FakePipeline(self)


def _request(run_id: str, interrupt_id: str, *, ttl: int) -> InterruptRequest:
    """Build a request that expires ``ttl`` seconds from now (negative = lapsed)."""
    now_ts = int(time.time())
    return InterruptRequest(
        interrupt_id=interrupt_id,
        run_id=run_id,
        step_name="review",
        message="needs a human",
        created_at=now_ts - 10,
        expires_at=now_ts + ttl,
    )


def _seed(fake: _FakeRedis, run_id: str, requests: list[InterruptRequest]) -> None:
    """Index every id and write a payload for each -- a consistent starting state."""
    fake.lists[_index_key(run_id)] = [req.interrupt_id for req in requests]
    for req in requests:
        fake.strings[_payload_key(run_id, req.interrupt_id)] = json.dumps(asdict(req))
    fake.writes.clear()


def _store(fake: _FakeRedis) -> RedisInterruptStore:
    return RedisInterruptStore(redis_url="redis://unused", redis_client=fake)


# --------------------------------------------------------------------------
# Negative controls: each of these fails against the pre-fix store.
# --------------------------------------------------------------------------


async def test_a_sweep_removes_the_expired_ids_from_every_run_index() -> None:
    """Expiry owns the index: a reaped payload's id must not survive in it."""
    fake = _FakeRedis()
    _seed(
        fake,
        "run-1",
        [_request("run-1", "i-live", ttl=600), _request("run-1", "i-lapsed", ttl=-1)],
    )
    _seed(fake, "run-2", [_request("run-2", "i-other-lapsed", ttl=-5)])
    store = _store(fake)

    removed = await store.sweep_expired()

    assert removed == 2
    assert fake.lists[_index_key("run-1")] == ["i-live"]
    assert _index_key("run-2") not in fake.lists, "an emptied index key is dropped"
    assert _payload_key("run-1", "i-lapsed") not in fake.strings
    assert _payload_key("run-1", "i-live") in fake.strings


async def test_a_sweep_drops_an_indexed_id_whose_payload_is_already_gone() -> None:
    """An id with no payload is unreachable garbage; the sweeper reconciles it away."""
    fake = _FakeRedis()
    _seed(fake, "run-1", [_request("run-1", "i-live", ttl=600)])
    fake.lists[_index_key("run-1")].append("i-ghost")  # indexed, never a payload

    removed = await _store(fake).sweep_expired()

    assert removed == 0, "an orphan id is hygiene, not an expired request"
    assert fake.lists[_index_key("run-1")] == ["i-live"]


async def test_a_save_landing_during_a_listing_is_not_lost() -> None:
    """The read path must not overwrite an index a concurrent writer just appended to.

    The pre-fix ``list_requests`` computed a healed id list from its ``LRANGE``
    and wrote it back, so a ``save_request`` that appended in between was
    overwritten -- a *live pending* interrupt vanishing from ``list_pending``
    while its payload key survived. Nobody is ever asked for that approval.
    """
    fake = _FakeRedis()
    _seed(fake, "run-1", [_request("run-1", "i-a", ttl=600)])
    fake.lists[_index_key("run-1")].append("i-ghost")  # forces the old self-heal
    store = _store(fake)
    arriving = _request("run-1", "i-new", ttl=600)

    await asyncio.gather(store.list_requests("run-1"), store.save_request(arriving))

    assert "i-new" in fake.lists[_index_key("run-1")]
    assert "i-new" in [req.interrupt_id for req in await store.list_requests("run-1")]


async def test_listing_requests_writes_nothing() -> None:
    """``list_requests`` is a read. A read that mutates is the race, not the fix."""
    fake = _FakeRedis()
    _seed(fake, "run-1", [_request("run-1", "i-a", ttl=600)])
    fake.lists[_index_key("run-1")].append("i-ghost")
    store = _store(fake)

    listed = await store.list_requests("run-1")

    assert [req.interrupt_id for req in listed] == ["i-a"], "the ghost is filtered out"
    assert fake.writes == [], f"the read path issued writes: {fake.writes}"
    assert fake.lists[_index_key("run-1")] == ["i-a", "i-ghost"], "index untouched by a read"


# --------------------------------------------------------------------------
# Preservation: these already hold before the fix and must keep holding.
# --------------------------------------------------------------------------


async def test_a_sweep_still_reaps_an_expired_payload_no_index_references() -> None:
    """The payload SCAN is why the sweeper cannot be index-driven only.

    ``save_request`` writes the payload *before* indexing it, and payload keys
    carry no redis TTL, so a stranded payload would otherwise live forever.
    """
    fake = _FakeRedis()
    lapsed = _request("run-1", "i-unindexed", ttl=-1)
    fake.strings[_payload_key("run-1", "i-unindexed")] = json.dumps(asdict(lapsed))

    removed = await _store(fake).sweep_expired()

    assert removed == 1
    assert fake.strings == {}


async def test_a_sweep_leaves_a_live_run_untouched() -> None:
    """Nothing expired means nothing written -- and the index key is never the target."""
    fake = _FakeRedis()
    _seed(fake, "run-1", [_request("run-1", "i-a", ttl=600), _request("run-1", "i-b", ttl=600)])

    removed = await _store(fake).sweep_expired()

    assert removed == 0
    assert fake.lists[_index_key("run-1")] == ["i-a", "i-b"]
    assert fake.writes == []


async def test_the_sweeper_survives_an_interrupt_id_that_collides_with_the_index_glob() -> None:
    """``…:request:requests`` matches ``…:run:*:requests`` too -- a payload, not a list.

    Reconciling it as an index would raise ``WRONGTYPE`` and abort the whole
    sweep. The payload reap still expires it.
    """
    fake = _FakeRedis()
    lapsed = _request("run-1", "requests", ttl=-1)
    _seed(fake, "run-1", [lapsed])

    removed = await _store(fake).sweep_expired()

    assert removed == 1
    assert fake.strings == {}
    assert fake.lists == {}


async def test_a_live_id_that_collides_with_the_index_glob_does_not_abort_the_sweep() -> None:
    """Same collision, nothing expired: the reconcile pass must not LRANGE a string."""
    fake = _FakeRedis()
    _seed(fake, "run-1", [_request("run-1", "requests", ttl=600)])

    removed = await _store(fake).sweep_expired()

    assert removed == 0
    assert fake.lists[_index_key("run-1")] == ["requests"]
    assert _payload_key("run-1", "requests") in fake.strings


async def test_expiry_is_read_from_the_payload_not_the_key_shape() -> None:
    """Run and interrupt ids may contain colons; de-indexing must still find the list."""
    fake = _FakeRedis()
    run_id = "tenant:a:run:7"
    _seed(fake, run_id, [_request(run_id, "step:1", ttl=-1), _request(run_id, "step:2", ttl=600)])

    removed = await _store(fake).sweep_expired()

    assert removed == 1
    assert fake.lists[_index_key(run_id)] == ["step:2"]


async def test_the_fake_lrange_rejects_a_string_key_like_redis_does() -> None:
    """Oracle check: the WRONGTYPE guard above is testing something real."""
    fake = _FakeRedis()
    fake.strings["k"] = "v"

    with pytest.raises(_WrongTypeError):
        await fake.lrange("k", 0, -1)
