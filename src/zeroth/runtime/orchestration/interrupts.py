from __future__ import annotations

import json
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
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
        """Remove all expired interrupt requests across all runs. Returns count removed."""


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

    async def _rewrite_list(self, key: str, values: list[str]) -> None:
        """Rewrite one redis list key from scratch."""
        client = await self._client()
        await client.delete(key)
        for value in values:
            await client.rpush(key, value)

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
        """Persist one interrupt request."""
        client = await self._client()
        await client.set(
            self._request_key(request.run_id, request.interrupt_id), json.dumps(asdict(request))
        )
        index_key = self._request_index_key(request.run_id)
        existing = [value for value in await client.lrange(index_key, 0, -1)]
        normalized = [
            current
            for current in (self._decode_text(value) for value in existing)
            if current is not None
        ]
        if request.interrupt_id not in normalized:
            await client.rpush(index_key, request.interrupt_id)

    async def get_request(self, run_id: str, interrupt_id: str) -> InterruptRequest | None:
        """Fetch one interrupt request by id."""
        client = await self._client()
        payload = self._decode_text(await client.get(self._request_key(run_id, interrupt_id)))
        if payload is None:
            return None
        return InterruptRequest(**json.loads(payload))

    async def list_requests(self, run_id: str) -> list[InterruptRequest]:
        """List all stored interrupt requests for a run."""
        client = await self._client()
        raw_ids = await client.lrange(self._request_index_key(run_id), 0, -1)
        normalized = [
            current
            for current in (self._decode_text(value) for value in raw_ids)
            if current is not None
        ]
        valid_ids: list[str] = []
        out: list[InterruptRequest] = []
        for interrupt_id in normalized:
            request = await self.get_request(run_id, interrupt_id)
            if request is None:
                continue
            valid_ids.append(interrupt_id)
            out.append(request)
        if valid_ids != normalized:
            await self._rewrite_list(self._request_index_key(run_id), valid_ids)
        return out

    async def delete_request(self, run_id: str, interrupt_id: str) -> None:
        """Delete one stored interrupt request."""
        client = await self._client()
        await client.delete(self._request_key(run_id, interrupt_id))
        raw_ids = await client.lrange(self._request_index_key(run_id), 0, -1)
        normalized = [
            current
            for current in (self._decode_text(value) for value in raw_ids)
            if current is not None
        ]
        filtered = [current for current in normalized if current != interrupt_id]
        await self._rewrite_list(self._request_index_key(run_id), filtered)

    async def sweep_expired(self) -> int:
        """Remove all expired interrupt requests across all runs. Returns count removed."""
        client = await self._client()
        now_ts = int(time.time())
        removed = 0
        cursor = 0
        pattern = f"{self.prefix}:run:*:request:*"
        while True:
            cursor, keys = await client.scan(cursor, match=pattern, count=100)
            for key in keys:
                payload = await client.get(key)
                if payload is None:
                    continue
                data = json.loads(payload)
                if data.get("expires_at", 0) > 0 and data["expires_at"] <= now_ts:
                    await client.delete(key)
                    removed += 1
            if cursor == 0:
                break
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
