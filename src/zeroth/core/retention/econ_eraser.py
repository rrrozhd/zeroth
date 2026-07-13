"""Econ-event erasure hook (WS-E).

The economic control plane (``zeroth.econ_plane``) records ``execution_events``
and ``outcome_events`` that carry tenant / cost / potentially-PII payloads on its
OWN SQLAlchemy database — separate from the core append-only audit DB. Right-to-
erasure must reach that data too, so the erasure service calls this interface.

Correlation caveat (the named deferred item — see docs/retention-and-erasure.md):
econ events are keyed by ``join_key``, a *business-request* identifier resolved
from runtime context, and there is NO durable ``run_id -> join_key`` index. The
erasure service therefore passes the run id plus the explicit top-level economic
``join_key`` from audit ``execution_metadata``. Nested payload keys are never
trusted as correlation authority. Every deletion is constrained by both tenant
and join key; automatic, complete run->join_key resolution remains deferred.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Protocol, runtime_checkable


@runtime_checkable
class EconEventEraser(Protocol):
    """Deletes one tenant's econ execution/outcome events for given join keys."""

    async def delete_events_for_run(
        self,
        tenant_id: str,
        join_keys: Sequence[str],
        *,
        idempotency_key: str,
    ) -> int:
        """Delete tenant events whose ``join_key`` is in ``join_keys``.

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

    async def delete_events_for_run(
        self,
        tenant_id: str,
        join_keys: Sequence[str],
        *,
        idempotency_key: str,
    ) -> int:
        keys = [k for k in dict.fromkeys(join_keys) if k]
        if not keys:
            return 0
        return await asyncio.to_thread(self._delete_sync, tenant_id, keys)

    def _delete_sync(self, tenant_id: str, keys: list[str]) -> int:
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
                    delete(model).where(
                        model.tenant_id == tenant_id,
                        model.join_key.in_(keys),
                    )
                )
                deleted += int(result.rowcount or 0)
            session.commit()
        finally:
            session.close()
        return deleted
