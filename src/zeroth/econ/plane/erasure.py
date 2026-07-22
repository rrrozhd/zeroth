"""Concrete econ-event erasure over the econ_plane SQLAlchemy models.

This is the econ plane's side of right-to-erasure: the
:class:`~zeroth.governance.retention.econ_eraser.EconEventEraser` protocol stays with
retention, which defines *what* erasure needs; this adapter lives with the
database it deletes from and is injected into the erasure service. Keeping it
here is what lets the governance domain not import ``zeroth.econ.plane``.

See the correlation caveat on the protocol module: deletions are constrained by
both tenant and explicit join keys, never by nested payload keys.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime


class SqlAlchemyEconEventEraser:
    """Concrete ``EconEventEraser`` over the econ_plane SQLAlchemy models.

    Imports ``zeroth.econ.plane`` lazily so a plain ``zeroth-core`` install
    without the ``regulus`` extra never pays for it. Not wired into the default
    boot path (see bootstrap) — instantiate and pass it to
    ``RetentionErasureService`` when the econ plane is present.
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
        return await asyncio.to_thread(self._delete_sync, tenant_id, keys, idempotency_key)

    def _delete_sync(
        self,
        tenant_id: str,
        keys: list[str],
        idempotency_key: str,
    ) -> int:
        # Lazy import: keeps the econ_plane dependency out of the base install.
        from sqlalchemy import delete

        from zeroth.econ.plane.database import SessionLocal
        from zeroth.econ.plane.instrumentation.models import (
            EconErasureReceipt,
            ExecutionEvent,
            OutcomeEvent,
        )

        deleted = 0
        session = SessionLocal()
        try:
            receipt = session.get(EconErasureReceipt, idempotency_key)
            if receipt is not None:
                if receipt.tenant_id != tenant_id:
                    raise ValueError("econ erasure idempotency key tenant mismatch")
                return int(receipt.deleted_count)
            for model in (ExecutionEvent, OutcomeEvent):
                result = session.execute(
                    delete(model).where(
                        model.tenant_id == tenant_id,
                        model.join_key.in_(keys),
                    )
                )
                deleted += int(result.rowcount or 0)
            session.add(
                EconErasureReceipt(
                    operation_id=idempotency_key,
                    tenant_id=tenant_id,
                    deleted_count=deleted,
                    created_at=datetime.now(UTC),
                )
            )
            session.commit()
        finally:
            session.close()
        return deleted
