from __future__ import annotations

import json
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from zeroth.contracts.graph.token_snapshot import TokenEngineSnapshot
    from zeroth.runtime.orchestration.token_lifecycle import TokenLifecycleAdapter


@dataclass
class InterruptRequest:
    interrupt_id: str
    run_id: str
    step_name: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)
    epoch: int = 0
    created_at: int = field(default_factory=lambda: int(time.time()))
    expires_at: int = 0
    status: str = "pending"


class InterruptExpiredError(Exception):
    """Raised when a resolve arrives after its interrupt request expired.

    ZER-25 note: this used to be imported from
    ``zeroth.contracts.governed.workflows.exceptions``, a module that does not exist
    in this repository -- the governai vendoring never included it. The import
    sat inside the resolve path, so an expired interrupt raised
    ``ModuleNotFoundError`` instead of this error. Defining it here makes the
    expiry path do what its surrounding code already says it does.
    """

    def __init__(self, message: str, *, request: object | None = None) -> None:
        super().__init__(message)
        self.request = request


@dataclass
class InterruptResolution:
    request: InterruptRequest
    response: Any
    resolved_at: int = field(default_factory=lambda: int(time.time()))


class InterruptStore(ABC):
    """Persistence backend for interrupt requests and per-run epochs."""

    @abstractmethod
    async def get_epoch(self, run_id: str) -> int:
        """Return the persisted epoch for a run."""

    @abstractmethod
    async def set_epoch(self, run_id: str, epoch: int) -> None:
        """Persist the epoch for a run."""

    @abstractmethod
    async def save_request(self, request: InterruptRequest) -> None:
        """Persist one interrupt request."""

    @abstractmethod
    async def get_request(self, run_id: str, interrupt_id: str) -> InterruptRequest | None:
        """Fetch one interrupt request by id."""

    @abstractmethod
    async def list_requests(self, run_id: str) -> list[InterruptRequest]:
        """List all stored interrupt requests for a run."""

    @abstractmethod
    async def delete_request(self, run_id: str, interrupt_id: str) -> None:
        """Delete one stored interrupt request."""

    @abstractmethod
    async def sweep_expired(self) -> int:
        """Remove all expired interrupt requests across all runs. Returns count removed.

        A backend that keeps an index of ids alongside the requests themselves
        must maintain it here: the component that owns expiry owns the index it
        invalidates. Offloading that onto readers is what forced a *read* path to
        mutate shared state (ZER-49 F-10).
        """


class InMemoryInterruptStore(InterruptStore):
    """In-memory interrupt persistence."""

    def __init__(self) -> None:
        """Initialize in-memory interrupt indexes."""
        self._requests: dict[str, dict[str, InterruptRequest]] = {}
        self._epochs: dict[str, int] = {}

    async def get_epoch(self, run_id: str) -> int:
        """Return the current epoch for a run."""
        return self._epochs.get(run_id, 0)

    async def set_epoch(self, run_id: str, epoch: int) -> None:
        """Persist the current epoch for a run."""
        self._epochs[run_id] = epoch

    async def save_request(self, request: InterruptRequest) -> None:
        """Persist one request object."""
        self._requests.setdefault(request.run_id, {})[request.interrupt_id] = request

    async def get_request(self, run_id: str, interrupt_id: str) -> InterruptRequest | None:
        """Fetch one request object by run and interrupt id."""
        return self._requests.get(run_id, {}).get(interrupt_id)

    async def list_requests(self, run_id: str) -> list[InterruptRequest]:
        """List all request objects for a run."""
        return list(self._requests.get(run_id, {}).values())

    async def delete_request(self, run_id: str, interrupt_id: str) -> None:
        """Delete one request object."""
        requests = self._requests.get(run_id)
        if requests is None:
            return
        requests.pop(interrupt_id, None)
        if not requests:
            self._requests.pop(run_id, None)

    async def sweep_expired(self) -> int:
        """Remove all expired interrupt requests across all runs. Returns count removed."""
        now_ts = int(time.time())
        removed = 0
        for run_id in list(self._requests.keys()):
            for interrupt_id, req in list(self._requests.get(run_id, {}).items()):
                if req.expires_at > 0 and req.expires_at <= now_ts:
                    del self._requests[run_id][interrupt_id]
                    removed += 1
            # Clean up empty run entries
            if run_id in self._requests and not self._requests[run_id]:
                del self._requests[run_id]
        return removed


class RedisInterruptStore(InterruptStore):
    """Redis-backed durable interrupt persistence."""

    def __init__(
        self,
        *,
        redis_url: str,
        prefix: str = "governai:interrupt",
        redis_client: Any | None = None,
    ) -> None:
        """Initialize Redis interrupt store configuration."""
        self.redis_url = redis_url
        self.prefix = prefix
        self._redis = redis_client

    async def _client(self) -> Any:
        """Return cached redis client, creating it lazily when needed."""
        if self._redis is not None:
            return self._redis
        try:
            import redis.asyncio as redis  # type: ignore
        except Exception as exc:  # pragma: no cover - dependency optional
            raise RuntimeError("RedisInterruptStore requires 'redis' package") from exc
        self._redis = redis.from_url(self.redis_url, encoding="utf-8", decode_responses=True)
        return self._redis

    @staticmethod
    def _decode_text(value: Any) -> str | None:
        """Normalize redis return values into text."""
        if value is None:
            return None
        if isinstance(value, bytes):
            return value.decode("utf-8")
        if isinstance(value, str):
            return value
        return str(value)

    def _epoch_key(self, run_id: str) -> str:
        """Build redis key for run epoch."""
        return f"{self.prefix}:run:{run_id}:epoch"

    def _request_key(self, run_id: str, interrupt_id: str) -> str:
        """Build redis key for one interrupt request."""
        return f"{self.prefix}:run:{run_id}:request:{interrupt_id}"

    def _request_index_key(self, run_id: str) -> str:
        """Build redis list key for interrupt ids in insertion order."""
        return f"{self.prefix}:run:{run_id}:requests"

    def _run_id_from_index_key(self, key: str) -> str | None:
        """Recover a run id from its index key, or ``None`` when the key is ambiguous.

        The index glob ``{prefix}:run:*:requests`` also matches the *payload* key
        of an interrupt whose id is literally ``requests``
        (``{prefix}:run:{run_id}:request:requests``), because redis ``*`` spans
        colons. Treating that string as a list would raise ``WRONGTYPE`` and
        abort the whole sweep, so the ambiguous shape is skipped here; the
        payload reap still expires it. A run id that genuinely ends in
        ``:request`` pays for this by not being reconciled -- readers already
        tolerate a stale id, and no id is ever wrongly dropped.
        """
        prefix = f"{self.prefix}:run:"
        suffix = ":requests"
        if not key.startswith(prefix) or not key.endswith(suffix):
            return None
        run_id = key[len(prefix) : -len(suffix)]
        if not run_id or run_id.endswith(":request"):
            return None
        return run_id

    async def _index_ids(self, index_key: str) -> list[str]:
        """Read one run's interrupt-id index as text, in insertion order."""
        client = await self._client()
        raw_ids = await client.lrange(index_key, 0, -1)
        return [
            current
            for current in (self._decode_text(value) for value in raw_ids)
            if current is not None
        ]

    async def _scan_keys(self, pattern: str) -> AsyncIterator[str]:
        """Yield every key matching ``pattern``, one SCAN page at a time."""
        client = await self._client()
        cursor = 0
        while True:
            cursor, keys = await client.scan(cursor, match=pattern, count=100)
            for key in keys:
                text = self._decode_text(key)
                if text is not None:
                    yield text
            if cursor == 0:
                break

    async def _rewrite_list(self, key: str, values: list[str]) -> None:
        """Rewrite one redis list key from scratch, in a single MULTI/EXEC.

        ZER-49 F-09: this is the sibling of ``RedisRunStore._rewrite_list``, and
        it carried the same defect after that one was fixed -- ``DELETE`` and a
        *loop* of ``RPUSH`` as separate round-trips, so any concurrent reader
        between them saw an empty or half-rebuilt interrupt index.

        MULTI/EXEC closes that torn read and nothing more. It does NOT close the
        lost-append race against ``save_request``: ``values`` is a snapshot the
        CALLER computed from an earlier LRANGE, so an interrupt id appended in
        between is still overwritten here.

        **This method has no callers, and that is the point.** Both it once had
        were removed rather than repaired, because the defect is the
        read-modify-write, not the write. ZER-49 F-10 took ``list_requests`` off
        it -- healing the index on a *read* could drop a live pending interrupt a
        concurrent ``save_request`` had just appended -- and F-11 took
        ``delete_request`` off it, onto a single ``LREM`` that removes one id
        without reading the list. Expiry (``sweep_expired``) owns index
        maintenance now.

        It is kept as the audited primitive for a future whole-index write, with
        its two non-obvious properties pinned in
        ``tests/runtime/orchestration/test_interrupt_store_rewrite_atomicity.py``.
        Before routing a path through it, check that path cannot race an append:
        if it can, it needs a WATCH established before its own read -- as
        ``RedisRunStore.put`` does -- or a targeted command, not this.

        Unlike the run-store version this applies no redis TTL: this store has
        no ``ttl_seconds`` and expires requests at the application layer
        (``InterruptRequest.expires_at`` plus ``sweep_expired``). Expiring the
        index alone would drop live interrupts out of ``list_pending`` while
        their payload keys survived.
        """
        client = await self._client()
        async with client.pipeline(transaction=True) as pipe:
            pipe.delete(key)
            if values:
                pipe.rpush(key, *values)
            await pipe.execute()

    async def get_epoch(self, run_id: str) -> int:
        """Return the persisted epoch for a run."""
        client = await self._client()
        payload = self._decode_text(await client.get(self._epoch_key(run_id)))
        if payload is None:
            return 0
        return int(payload)

    async def set_epoch(self, run_id: str, epoch: int) -> None:
        """Persist the current epoch for a run."""
        client = await self._client()
        await client.set(self._epoch_key(run_id), str(int(epoch)))

    async def save_request(self, request: InterruptRequest) -> None:
        """Persist one interrupt request.

        The payload is written *before* the id is indexed, and that order is load
        bearing: ``sweep_expired`` treats an indexed id with no payload as
        garbage to reconcile away, so indexing first would let a sweep delete an
        id whose payload was still in flight.
        """
        client = await self._client()
        await client.set(
            self._request_key(request.run_id, request.interrupt_id), json.dumps(asdict(request))
        )
        index_key = self._request_index_key(request.run_id)
        if request.interrupt_id not in await self._index_ids(index_key):
            await client.rpush(index_key, request.interrupt_id)

    async def get_request(self, run_id: str, interrupt_id: str) -> InterruptRequest | None:
        """Fetch one interrupt request by id."""
        client = await self._client()
        payload = self._decode_text(await client.get(self._request_key(run_id, interrupt_id)))
        if payload is None:
            return None
        return InterruptRequest(**json.loads(payload))

    async def list_requests(self, run_id: str) -> list[InterruptRequest]:
        """List all stored interrupt requests for a run.

        ZER-49 F-10: this is a pure read. It filters out ids whose payload is
        gone rather than rewriting the index to match, because that rewrite was a
        read-modify-write on shared state: an id appended by a concurrent
        ``save_request`` between the LRANGE and the rewrite was overwritten, so a
        *live pending* interrupt disappeared from ``list_pending`` and
        ``get_latest_pending`` while its payload key survived -- an approval a
        human was waiting on that nobody would ever be asked for.

        Skipping such an id is enough for a correct listing; removing it is
        expiry's job, and ``sweep_expired`` now does it.
        """
        out: list[InterruptRequest] = []
        for interrupt_id in await self._index_ids(self._request_index_key(run_id)):
            request = await self.get_request(run_id, interrupt_id)
            if request is not None:
                out.append(request)
        return out

    async def delete_request(self, run_id: str, interrupt_id: str) -> None:
        """Delete one stored interrupt request, payload and index entry together.

        ZER-49 F-11: this was the last lost-append site in the store. It used to
        ``LRANGE`` the index, compute the remainder and write it back through
        ``_rewrite_list``; a ``save_request`` that appended between the read and
        the write was overwritten, leaving a *live pending* interrupt whose
        payload key survived but whose id no longer appeared in ``list_pending``
        or ``get_latest_pending``. Nobody is ever asked for that approval.

        ``LREM`` removes exactly the id being deleted, in one command against the
        live list, so an append it never read cannot be clobbered. The count is
        ``0`` -- every occurrence -- because that is what the filter it replaces
        already did; ``count=1`` would be a behaviour change smuggled in by a bug
        fix, leaving a duplicate id pointing at a payload just deleted. It also
        makes the removal idempotent by identity, which matters because
        ``save_request`` decides whether to append by reading the index first and
        so can double-index one id under concurrency.

        Nothing is lost by dropping the rewrite: it normalized nothing. Its input
        was the whole index minus this id, written back verbatim -- it deduped
        nothing, dropped no stale entry, and never checked whether the remaining
        ids still had payloads. Reconciling ids whose payload is gone belongs to
        ``sweep_expired``, which now does it explicitly.

        Payload and index update share one ``MULTI``/``EXEC`` so there is no
        window between them at all. That is not merely a saved round-trip: the
        de-index-first order strands a payload key, and a payload with
        ``expires_at <= 0`` is one ``sweep_expired`` never reaps -- an unbounded
        leak, since payload keys carry no redis TTL. Pipelining makes the
        ordering question moot rather than answering it.
        """
        client = await self._client()
        async with client.pipeline(transaction=True) as pipe:
            pipe.delete(self._request_key(run_id, interrupt_id))
            pipe.lrem(self._request_index_key(run_id), 0, interrupt_id)
            await pipe.execute()

    @staticmethod
    def _expired_request(payload: str, now_ts: int) -> dict[str, Any] | None:
        """Decode one stored request, returning it only when it has expired.

        Returns ``None`` for anything this sweep must not act on: a key the
        payload glob matched but that does not hold a serialized request, and a
        request that is still live. One unparsable key must not abort the sweep.
        """
        try:
            data = json.loads(payload)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        expires_at = data.get("expires_at", 0)
        if not isinstance(expires_at, int | float) or expires_at <= 0:
            return None
        return data if expires_at <= now_ts else None

    async def sweep_expired(self) -> int:
        """Remove all expired interrupt requests across all runs. Returns count removed.

        ZER-49 F-10. This used to SCAN ``{prefix}:run:*:request:*`` and delete
        the matching payload keys -- and nothing else. The index key is
        ``{prefix}:run:{run_id}:requests``, with no colon after ``request``, so
        the glob never matched it and the ids of everything this reaped stayed in
        the index. Every reader inherited that dirt, which is why ``list_requests``
        used to heal the index on a read path. Fixing the *glob* would not have
        helped: a matching index key would be fed to GET and json.loads as if it
        were a request. The defect was the missing index maintenance.

        Two passes, each with one job:

        1. **Reap.** SCAN the payload keys, delete the expired ones, and LREM
           each id from its run's index. Run and interrupt id come from the
           payload, not from parsing the key, so ids containing colons de-index
           correctly. LREM is a single atomic command against the live list --
           unlike a rewrite it cannot lose a concurrent append.
        2. **Reconcile.** SCAN the index keys and LREM any id with no payload:
           entries stranded by an interrupted delete, an evicted payload, or the
           reap above. ``save_request`` writes the payload before indexing the
           id, so an id that is merely mid-save is never mistaken for garbage.

        The payload SCAN stays because the index is not authoritative over the
        payloads: an id can be missing from the index while its payload key
        lives on, and payload keys carry no redis TTL, so an index-driven sweep
        would leak them forever.

        The count is expired *requests* reaped. De-indexing an orphan id is
        hygiene, not an expiry, and is not counted.
        """
        client = await self._client()
        now_ts = int(time.time())
        removed = 0
        async for key in self._scan_keys(f"{self.prefix}:run:*:request:*"):
            payload = self._decode_text(await client.get(key))
            if payload is None:
                continue
            data = self._expired_request(payload, now_ts)
            if data is None:
                continue
            await client.delete(key)
            removed += 1
            run_id = data.get("run_id")
            interrupt_id = data.get("interrupt_id")
            if isinstance(run_id, str) and isinstance(interrupt_id, str):
                await client.lrem(self._request_index_key(run_id), 0, interrupt_id)
        async for index_key in self._scan_keys(f"{self.prefix}:run:*:requests"):
            run_id = self._run_id_from_index_key(index_key)
            if run_id is None:
                continue
            for interrupt_id in await self._index_ids(index_key):
                if await client.get(self._request_key(run_id, interrupt_id)) is None:
                    await client.lrem(index_key, 0, interrupt_id)
        return removed

    async def close(self) -> None:
        """Close underlying redis client."""
        if self._redis is None:
            return
        aclose = getattr(self._redis, "aclose", None)
        if callable(aclose):
            await aclose()


class InterruptManager:
    """Tracks non-approval interrupts with TTL and epoch guards."""

    def __init__(
        self,
        *,
        default_ttl_seconds: int = 1800,
        store: InterruptStore | None = None,
        token_lifecycle: TokenLifecycleAdapter | None = None,
    ) -> None:
        """Initialize InterruptManager."""
        self.default_ttl_seconds = default_ttl_seconds
        self.store = store or InMemoryInterruptStore()
        self.token_lifecycle = token_lifecycle

    def _token_lifecycle(self) -> TokenLifecycleAdapter:
        if self.token_lifecycle is None:
            raise RuntimeError("TokenLifecycleAdapter is required for token lifecycle commands")
        return self.token_lifecycle

    async def pause_run(self, run_id: str) -> TokenEngineSnapshot:
        """Durably pause structured-token dispatch for one run."""
        return await self._token_lifecycle().pause(run_id)

    async def resume_run(self, run_id: str) -> TokenEngineSnapshot:
        """Resume a paused or stopped structured-token snapshot."""
        return await self._token_lifecycle().resume(run_id)

    async def cancel_run(self, run_id: str) -> TokenEngineSnapshot:
        """Fence and settle structured-token work for one run."""
        return await self._token_lifecycle().cancel(run_id)

    async def current_epoch(self, run_id: str) -> int:
        """Current epoch."""
        return await self.store.get_epoch(run_id)

    async def bump_epoch(self, run_id: str) -> int:
        """Bump epoch."""
        next_epoch = await self.current_epoch(run_id) + 1
        await self.store.set_epoch(run_id, next_epoch)
        return next_epoch

    async def create(
        self,
        *,
        run_id: str,
        step_name: str,
        message: str,
        context: dict[str, Any] | None = None,
        ttl_seconds: int | None = None,
        epoch: int | None = None,
        max_pending: int | None = None,
    ) -> InterruptRequest:
        """Create."""
        if max_pending is not None and max_pending >= 0:
            current_pending = await self.list_pending(run_id, epoch=epoch)
            if len(current_pending) >= max_pending:
                raise ValueError(f"Run {run_id} reached max pending interrupts ({max_pending})")
        now_ts = int(time.time())
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl_seconds
        resolved_epoch = await self.current_epoch(run_id) if epoch is None else epoch
        request = InterruptRequest(
            interrupt_id=str(uuid.uuid4()),
            run_id=run_id,
            step_name=step_name,
            message=message,
            context=dict(context or {}),
            epoch=resolved_epoch,
            created_at=now_ts,
            expires_at=now_ts + int(ttl),
        )
        await self.store.save_request(request)
        return request

    async def get_pending(self, run_id: str, interrupt_id: str) -> InterruptRequest | None:
        """Return one pending interrupt request when it still exists and is live."""
        req = await self.store.get_request(run_id, interrupt_id)
        if req is None or req.status != "pending":
            return None
        if req.expires_at <= int(time.time()):
            req.status = "expired"
            await self.store.save_request(req)
            return None
        return req

    async def get_latest_pending(
        self, run_id: str, *, epoch: int | None = None
    ) -> InterruptRequest | None:
        """Return the newest pending interrupt for a run."""
        pending = await self.list_pending(run_id, epoch=epoch)
        if not pending:
            return None
        return pending[-1]

    async def list_pending(
        self, run_id: str, *, epoch: int | None = None
    ) -> list[InterruptRequest]:
        """List pending."""
        now_ts = int(time.time())
        requests = await self.store.list_requests(run_id)
        out: list[InterruptRequest] = []
        for req in requests:
            if req.status != "pending":
                continue
            if req.expires_at <= now_ts:
                req.status = "expired"
                await self.store.save_request(req)
                continue
            if epoch is not None and req.epoch != epoch:
                continue
            out.append(req)
        out.sort(key=lambda item: (item.created_at, item.interrupt_id))
        return out

    async def resolve(
        self,
        *,
        run_id: str,
        interrupt_id: str,
        response: Any,
        epoch: int | None = None,
    ) -> InterruptResolution:
        """Resolve."""
        req = await self.store.get_request(run_id, interrupt_id)
        if req is None:
            raise KeyError(f"Unknown interrupt_id: {interrupt_id}")
        if req.status != "pending":
            raise ValueError(f"Interrupt {interrupt_id} is not pending")
        if req.expires_at <= int(time.time()):
            req.status = "expired"
            await self.store.save_request(req)
            raise InterruptExpiredError(f"Interrupt {interrupt_id} has expired", request=req)
        if epoch is not None and req.epoch != epoch:
            raise ValueError(
                f"Interrupt epoch mismatch for {interrupt_id}: "
                f"expected={req.epoch} provided={epoch}"
            )
        req.status = "resolved"
        await self.store.save_request(req)
        return InterruptResolution(request=req, response=response)

    async def clear_expired(self, run_id: str) -> int:
        """Clear expired and resolved requests for one run."""
        now_ts = int(time.time())
        removed = 0
        for req in await self.store.list_requests(run_id):
            if req.status != "pending" or req.expires_at <= now_ts:
                await self.store.delete_request(run_id, req.interrupt_id)
                removed += 1
        return removed
