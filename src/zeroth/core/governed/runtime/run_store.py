from __future__ import annotations

import asyncio
import uuid
from abc import ABC, abstractmethod
from typing import Any, Protocol, runtime_checkable

from zeroth.contracts.governed.models.run_state import RunState

try:
    from redis.exceptions import WatchError
except ImportError:

    class WatchError(Exception):  # type: ignore[no-redef]
        pass


class StateConcurrencyError(RuntimeError):
    """Raised when an optimistic lock conflict exhausts retries."""


def _validate_state(state: RunState) -> None:
    """Validate state consistency before persistence (PERS-03)."""
    from zeroth.contracts.governed.models.common import RunStatus

    if state.status == RunStatus.WAITING_INTERRUPT and state.pending_interrupt_id is None:
        raise ValueError(
            f"Run {state.run_id}: status is WAITING_INTERRUPT but pending_interrupt_id is None"
        )
    if state.status == RunStatus.WAITING_APPROVAL and state.pending_approval is None:
        raise ValueError(
            f"Run {state.run_id}: status is WAITING_APPROVAL but pending_approval is None"
        )


@runtime_checkable
class ThreadAwareRunStore(Protocol):
    """Optional capability for thread-native run lookup and indexing."""

    async def get_active_run_id(self, thread_id: str) -> str | None:
        """Return the active run id for a thread, if any."""

    async def get_latest_run_id(self, thread_id: str) -> str | None:
        """Return the newest persisted run id for a thread, if any."""

    async def list_run_ids(self, thread_id: str) -> list[str]:
        """List persisted run ids for a thread in oldest-to-newest order."""

    async def set_active_run_id(self, thread_id: str, run_id: str) -> None:
        """Mark a run id as active for the given thread."""

    async def clear_active_run_id(self, thread_id: str, run_id: str) -> None:
        """Clear the active mapping when it still points at the given run id."""


class RunStore(ABC):
    """Persistence interface for workflow run state."""

    @abstractmethod
    async def put(self, state: RunState) -> None:
        """Atomically persist a run state snapshot.

        Validates state consistency before writing. Raises StateConcurrencyError
        if optimistic lock conflict cannot be resolved after retries.
        """

    @abstractmethod
    async def get(self, run_id: str) -> RunState | None:
        """Fetch a run state snapshot."""

    @abstractmethod
    async def delete(self, run_id: str) -> None:
        """Delete persisted run state."""

    @abstractmethod
    async def write_checkpoint(self, state: RunState) -> str:
        """Persist one checkpoint snapshot and return checkpoint id."""

    @abstractmethod
    async def get_checkpoint(self, checkpoint_id: str) -> RunState | None:
        """Fetch checkpoint snapshot by checkpoint id."""

    @abstractmethod
    async def get_latest_checkpoint(self, thread_id: str) -> RunState | None:
        """Fetch latest checkpoint for a thread."""

    @abstractmethod
    async def list_checkpoints(self, thread_id: str) -> list[RunState]:
        """List checkpoints for a thread ordered by write sequence."""


class InMemoryRunStore(RunStore):
    """In-memory run store suitable for tests and local runs."""

    def __init__(self) -> None:
        """Initialize in-memory state and indexes."""
        self._state: dict[str, RunState] = {}
        self._checkpoints: dict[str, RunState] = {}
        self._thread_checkpoints: dict[str, list[str]] = {}
        self._thread_runs: dict[str, list[str]] = {}
        self._thread_active: dict[str, str] = {}

    def _record_thread_run(self, thread_id: str, run_id: str) -> None:
        """Append a run id to thread history exactly once."""
        thread_runs = self._thread_runs.setdefault(thread_id, [])
        if run_id not in thread_runs:
            thread_runs.append(run_id)

    def _remove_thread_run(self, thread_id: str, run_id: str) -> None:
        """Remove one run id from thread history and clean up empty indexes."""
        thread_runs = self._thread_runs.get(thread_id)
        if thread_runs is None:
            return
        filtered = [current for current in thread_runs if current != run_id]
        if filtered:
            self._thread_runs[thread_id] = filtered
        else:
            self._thread_runs.pop(thread_id, None)

    async def put(self, state: RunState) -> None:
        """Persist latest run snapshot with epoch-based CAS and validation."""
        _validate_state(state)
        existing = self._state.get(state.run_id)
        if existing is not None and existing.epoch > state.epoch:
            raise StateConcurrencyError(
                f"Stale write for run {state.run_id}: "
                f"store epoch={existing.epoch}, write epoch={state.epoch}"
            )
        new_epoch = (existing.epoch if existing is not None else 0) + 1
        state.epoch = new_epoch
        checkpoint_id = await self.write_checkpoint(state)
        state.checkpoint_id = checkpoint_id
        self._state[state.run_id] = state.model_copy(deep=True)
        self._record_thread_run(state.thread_id, state.run_id)

    async def get(self, run_id: str) -> RunState | None:
        """Return a deep-copied run state snapshot by run id."""
        value = self._state.get(run_id)
        if value is None:
            return None
        return value.model_copy(deep=True)

    async def delete(self, run_id: str) -> None:
        """Delete persisted run state for the given run id."""
        value = self._state.pop(run_id, None)
        if value is None:
            return
        self._remove_thread_run(value.thread_id, run_id)
        if self._thread_active.get(value.thread_id) == run_id:
            self._thread_active.pop(value.thread_id, None)

    async def write_checkpoint(self, state: RunState) -> str:
        """Persist checkpoint snapshot and append it to the thread index."""
        checkpoint_id = state.checkpoint_id or str(uuid.uuid4())
        snapshot = state.model_copy(deep=True)
        snapshot.checkpoint_id = checkpoint_id
        self._checkpoints[checkpoint_id] = snapshot
        thread_list = self._thread_checkpoints.setdefault(state.thread_id, [])
        if checkpoint_id not in thread_list:
            thread_list.append(checkpoint_id)
        return checkpoint_id

    async def get_checkpoint(self, checkpoint_id: str) -> RunState | None:
        """Return checkpoint snapshot by id."""
        value = self._checkpoints.get(checkpoint_id)
        if value is None:
            return None
        return value.model_copy(deep=True)

    async def get_latest_checkpoint(self, thread_id: str) -> RunState | None:
        """Return the newest checkpoint snapshot for a thread id."""
        checkpoint_ids = self._thread_checkpoints.get(thread_id, [])
        if not checkpoint_ids:
            return None
        latest = self._checkpoints.get(checkpoint_ids[-1])
        if latest is None:
            return None
        return latest.model_copy(deep=True)

    async def list_checkpoints(self, thread_id: str) -> list[RunState]:
        """List all checkpoint snapshots for a thread in write order."""
        checkpoint_ids = self._thread_checkpoints.get(thread_id, [])
        out: list[RunState] = []
        for checkpoint_id in checkpoint_ids:
            snapshot = self._checkpoints.get(checkpoint_id)
            if snapshot is not None:
                out.append(snapshot.model_copy(deep=True))
        return out

    async def get_active_run_id(self, thread_id: str) -> str | None:
        """Return the active run id for a thread when it still exists."""
        run_id = self._thread_active.get(thread_id)
        if run_id is None:
            return None
        if run_id not in self._state:
            self._thread_active.pop(thread_id, None)
            return None
        return run_id

    async def get_latest_run_id(self, thread_id: str) -> str | None:
        """Return the newest persisted run id for a thread."""
        run_ids = await self.list_run_ids(thread_id)
        if not run_ids:
            return None
        return run_ids[-1]

    async def list_run_ids(self, thread_id: str) -> list[str]:
        """List thread run ids in insertion order, filtering deleted entries."""
        run_ids = self._thread_runs.get(thread_id, [])
        valid = [run_id for run_id in run_ids if run_id in self._state]
        if len(valid) != len(run_ids):
            if valid:
                self._thread_runs[thread_id] = valid
            else:
                self._thread_runs.pop(thread_id, None)
        return list(valid)

    async def set_active_run_id(self, thread_id: str, run_id: str) -> None:
        """Mark a run id as the current active run for the thread."""
        self._record_thread_run(thread_id, run_id)
        self._thread_active[thread_id] = run_id

    async def clear_active_run_id(self, thread_id: str, run_id: str) -> None:
        """Clear the active mapping only when it still points at the run id."""
        if self._thread_active.get(thread_id) == run_id:
            self._thread_active.pop(thread_id, None)


class RedisRunStore(RunStore):
    """Redis-backed run store.

    The redis client is optional and can be injected for testing.
    """

    def __init__(
        self,
        *,
        redis_url: str,
        prefix: str = "governai:run",
        ttl_seconds: int | None = None,
        redis_client: Any | None = None,
    ) -> None:
        """Initialize Redis-backed run store configuration."""
        self.redis_url = redis_url
        self.prefix = prefix
        self.ttl_seconds = ttl_seconds
        self._redis = redis_client

    async def _client(self) -> Any:
        """Return cached redis client, creating it lazily when needed."""
        if self._redis is not None:
            return self._redis
        try:
            import redis.asyncio as redis  # type: ignore
        except Exception as exc:  # pragma: no cover - dependency optional
            raise RuntimeError(
                "RedisRunStore requires 'redis' package (redis.asyncio)"
            ) from exc
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

    async def _maybe_expire(self, *keys: str) -> None:
        """Apply ttl to secondary keys when configured."""
        if self.ttl_seconds is None:
            return
        client = await self._client()
        for key in keys:
            await client.expire(key, int(self.ttl_seconds))

    def _key(self, run_id: str) -> str:
        """Build redis key for a run snapshot."""
        return f"{self.prefix}:{run_id}"

    def _checkpoint_key(self, checkpoint_id: str) -> str:
        """Build redis key for a checkpoint snapshot."""
        return f"{self.prefix}:checkpoint:{checkpoint_id}"

    def _thread_checkpoint_index_key(self, thread_id: str) -> str:
        """Build redis list key that tracks checkpoint ids for one thread."""
        return f"{self.prefix}:thread:{thread_id}:checkpoints"

    def _thread_run_index_key(self, thread_id: str) -> str:
        """Build redis list key that tracks run ids for one thread."""
        return f"{self.prefix}:thread:{thread_id}:runs"

    def _thread_active_key(self, thread_id: str) -> str:
        """Build redis key that tracks the active run for one thread."""
        return f"{self.prefix}:thread:{thread_id}:active"

    async def _rewrite_list(self, key: str, values: list[str]) -> None:
        """Rewrite one redis list key from scratch."""
        client = await self._client()
        await client.delete(key)
        if values:
            for value in values:
                await client.rpush(key, value)
            await self._maybe_expire(key)

    async def _record_thread_run(self, thread_id: str, run_id: str) -> None:
        """Append a run id to thread history exactly once."""
        client = await self._client()
        key = self._thread_run_index_key(thread_id)
        existing = [value for value in await client.lrange(key, 0, -1)]
        normalized = [current for current in (self._decode_text(value) for value in existing) if current is not None]
        if run_id not in normalized:
            await client.rpush(key, run_id)
        await self._maybe_expire(key)

    async def put(self, state: RunState) -> None:
        """Atomically persist run snapshot using WATCH/MULTI/EXEC with retry."""
        _validate_state(state)
        client = await self._client()
        run_key = self._key(state.run_id)
        checkpoint_id = state.checkpoint_id or str(uuid.uuid4())
        retries = 3
        delay = 0.05

        for attempt in range(retries + 1):
            async with client.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(run_key)
                    raw = await pipe.get(run_key)
                    if raw is not None:
                        text = self._decode_text(raw)
                        if text:
                            existing = RunState.model_validate_json(text)
                            if existing.epoch > state.epoch:
                                raise StateConcurrencyError(
                                    f"Stale write for run {state.run_id}: "
                                    f"store epoch={existing.epoch}, write epoch={state.epoch}"
                                )
                            state.epoch = existing.epoch + 1
                    else:
                        state.epoch = 1
                    state.checkpoint_id = checkpoint_id
                    payload = state.model_dump_json()
                    pipe.multi()
                    if self.ttl_seconds is None:
                        pipe.set(run_key, payload)
                    else:
                        pipe.set(run_key, payload, ex=int(self.ttl_seconds))
                    cp_key = self._checkpoint_key(checkpoint_id)
                    if self.ttl_seconds is None:
                        pipe.set(cp_key, payload)
                    else:
                        pipe.set(cp_key, payload, ex=int(self.ttl_seconds))
                    pipe.rpush(
                        self._thread_checkpoint_index_key(state.thread_id),
                        checkpoint_id,
                    )
                    await pipe.execute()
                    break
                except StateConcurrencyError:
                    raise
                except WatchError:
                    if attempt >= retries:
                        raise StateConcurrencyError(
                            f"Optimistic lock conflict on run {state.run_id} "
                            f"after {retries} retries"
                        ) from None
                    await asyncio.sleep(delay * (2**attempt))
        await self._maybe_expire(self._thread_checkpoint_index_key(state.thread_id))
        await self._record_thread_run(state.thread_id, state.run_id)

    async def get(self, run_id: str) -> RunState | None:
        """Fetch and decode one run snapshot by run id."""
        client = await self._client()
        payload = await client.get(self._key(run_id))
        if payload is None:
            return None
        text = self._decode_text(payload)
        if text is None:
            return None
        return RunState.model_validate_json(text)

    async def delete(self, run_id: str) -> None:
        """Delete persisted run snapshot by run id and clean thread indexes."""
        state = await self.get(run_id)
        client = await self._client()
        await client.delete(self._key(run_id))
        if state is None:
            return
        history_key = self._thread_run_index_key(state.thread_id)
        raw_history = [value for value in await client.lrange(history_key, 0, -1)]
        normalized = [current for current in (self._decode_text(value) for value in raw_history) if current is not None]
        filtered = [current for current in normalized if current != run_id]
        await self._rewrite_list(history_key, filtered)
        active_key = self._thread_active_key(state.thread_id)
        active_run_id = self._decode_text(await client.get(active_key))
        if active_run_id == run_id:
            await client.delete(active_key)

    async def write_checkpoint(self, state: RunState) -> str:
        """Write checkpoint payload and append checkpoint id to thread index."""
        client = await self._client()
        checkpoint_id = state.checkpoint_id or str(uuid.uuid4())
        payload_state = state.model_copy(deep=True)
        payload_state.checkpoint_id = checkpoint_id
        payload = payload_state.model_dump_json()
        key = self._checkpoint_key(checkpoint_id)
        if self.ttl_seconds is None:
            await client.set(key, payload)
        else:
            await client.set(key, payload, ex=int(self.ttl_seconds))
        checkpoint_index_key = self._thread_checkpoint_index_key(state.thread_id)
        await client.rpush(checkpoint_index_key, checkpoint_id)
        await self._maybe_expire(checkpoint_index_key)
        return checkpoint_id

    async def get_checkpoint(self, checkpoint_id: str) -> RunState | None:
        """Fetch and decode one checkpoint snapshot by checkpoint id."""
        client = await self._client()
        payload = await client.get(self._checkpoint_key(checkpoint_id))
        if payload is None:
            return None
        text = self._decode_text(payload)
        if text is None:
            return None
        return RunState.model_validate_json(text)

    async def get_latest_checkpoint(self, thread_id: str) -> RunState | None:
        """Fetch most recent checkpoint snapshot for a thread id."""
        client = await self._client()
        checkpoint_id = self._decode_text(await client.lindex(self._thread_checkpoint_index_key(thread_id), -1))
        if checkpoint_id is None:
            return None
        return await self.get_checkpoint(checkpoint_id)

    async def list_checkpoints(self, thread_id: str) -> list[RunState]:
        """Fetch all checkpoints for a thread in insertion order."""
        client = await self._client()
        checkpoint_ids = await client.lrange(self._thread_checkpoint_index_key(thread_id), 0, -1)
        out: list[RunState] = []
        for checkpoint_id in checkpoint_ids:
            text = self._decode_text(checkpoint_id)
            if text is None:
                continue
            snapshot = await self.get_checkpoint(text)
            if snapshot is not None:
                out.append(snapshot)
        return out

    async def get_active_run_id(self, thread_id: str) -> str | None:
        """Return the active run id for a thread when it still exists."""
        client = await self._client()
        run_id = self._decode_text(await client.get(self._thread_active_key(thread_id)))
        if run_id is None:
            return None
        if await self.get(run_id) is None:
            await client.delete(self._thread_active_key(thread_id))
            return None
        return run_id

    async def get_latest_run_id(self, thread_id: str) -> str | None:
        """Return the newest persisted run id for a thread."""
        run_ids = await self.list_run_ids(thread_id)
        if not run_ids:
            return None
        return run_ids[-1]

    async def list_run_ids(self, thread_id: str) -> list[str]:
        """List thread run ids in insertion order, filtering stale entries."""
        client = await self._client()
        key = self._thread_run_index_key(thread_id)
        raw_history = await client.lrange(key, 0, -1)
        normalized = [current for current in (self._decode_text(value) for value in raw_history) if current is not None]
        unique_in_order: list[str] = []
        seen: set[str] = set()
        for run_id in normalized:
            if run_id in seen:
                continue
            seen.add(run_id)
            unique_in_order.append(run_id)
        valid: list[str] = []
        for run_id in unique_in_order:
            if await self.get(run_id) is not None:
                valid.append(run_id)
        if valid != normalized:
            await self._rewrite_list(key, valid)
        return valid

    async def set_active_run_id(self, thread_id: str, run_id: str) -> None:
        """Mark a run id as active for the thread."""
        await self._record_thread_run(thread_id, run_id)
        client = await self._client()
        if self.ttl_seconds is None:
            await client.set(self._thread_active_key(thread_id), run_id)
        else:
            await client.set(self._thread_active_key(thread_id), run_id, ex=int(self.ttl_seconds))

    async def clear_active_run_id(self, thread_id: str, run_id: str) -> None:
        """Clear the active mapping only when it still points at the run id."""
        client = await self._client()
        key = self._thread_active_key(thread_id)
        active_run_id = self._decode_text(await client.get(key))
        if active_run_id == run_id:
            await client.delete(key)

    async def aclose(self) -> None:
        """Close underlying redis client if it exposes an async close hook."""
        if self._redis is None:
            return
        close = getattr(self._redis, "aclose", None)
        if callable(close):
            await close()
