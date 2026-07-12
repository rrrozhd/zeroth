"""Econ-event erasure hook (WS-E).

The economic control plane (``zeroth.econ_plane``) records ``execution_events``
and ``outcome_events`` that carry tenant / cost / potentially-PII payloads on its
OWN SQLAlchemy database — separate from the core append-only audit DB. Right-to-
erasure must reach that data too, so the erasure service calls this interface.

Correlation caveat (the named deferred item — see docs/retention-and-erasure.md):
econ events are keyed by ``join_key``, a *business-request* identifier resolved
from runtime context, and there is NO durable ``run_id -> join_key`` index. The
erasure service therefore passes the best-effort join keys it can derive from the
run (its ``run_id`` plus any ``join_key`` found in audit ``execution_metadata``).
Automatic, complete run->join_key resolution is deferred; this hook deletes
exactly the events whose ``join_key`` matches what it is handed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Protocol, runtime_checkable


@runtime_checkable
class EconEventEraser(Protocol):
    """Deletes econ execution/outcome events for the given join keys."""

    async def delete_events_for_run(self, join_keys: Sequence[str]) -> int:
        """Delete all econ events whose ``join_key`` is in ``join_keys``.

        Returns the total number of rows deleted across event tables.
        """
        ...


class SqlAlchemyEconEventEraser:
    """Concrete :class:`EconEventEraser` over the econ_plane SQLAlchemy models.

    Imports ``zeroth.econ_plane`` lazily so a plain ``zeroth-core`` install
    without the ``regulus`` extra never pays for it. Not wired into the default
    boot path (see bootstrap) — instantiate and pass it to
    :class:`RetentionErasureService` when the econ plane is present.
    """

    async def delete_events_for_run(self, join_keys: Sequence[str]) -> int:
        keys = [k for k in dict.fromkeys(join_keys) if k]
        if not keys:
            return 0
        return await asyncio.to_thread(self._delete_sync, keys)

    def _delete_sync(self, keys: list[str]) -> int:
        # Lazy import: keeps the econ_plane dependency out of the base install.
        from sqlalchemy import delete

        from zeroth.econ_plane.database import SessionLocal
        from zeroth.econ_plane.instrumentation.models import (
            ExecutionEvent,
            OutcomeEvent,
        )

        deleted = 0
        session = SessionLocal()
        try:
            for model in (ExecutionEvent, OutcomeEvent):
                result = session.execute(
                    delete(model).where(model.join_key.in_(keys))
                )
                deleted += int(result.rowcount or 0)
            session.commit()
        finally:
            session.close()
        return deleted
