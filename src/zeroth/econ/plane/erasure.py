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
from collections.abc import Callable, Sequence
from datetime import UTC, datetime


class SqlAlchemyEconEventEraser:
    """Concrete ``EconEventEraser`` over the econ_plane SQLAlchemy models.

    Imports ``zeroth.econ.plane`` lazily for standalone/legacy construction so
    a plain ``zeroth-core`` install without the ``regulus`` extra never pays for
    it. Live service bootstrap supplies the configured econ session factory
    explicitly and injects the adapter into ``RetentionErasureService``.
    """

    def __init__(self, session_factory: Callable[[], object] | None = None) -> None:
        """Bind cleanup to one configured econ database session factory.

        ``None`` preserves the standalone/legacy construction path, which
        resolves the econ plane's configured ``SessionLocal`` lazily. Service
        bootstrap injects that factory explicitly so a destructive cleanup can
        never drift to a different module-global database after construction.
        """
        self._session_factory = session_factory

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

    @staticmethod
    def _replay(session: object, receipt_model: type, tenant_id: str, key: str) -> int | None:
        """Return the count a completed receipt already recorded for this key."""
        receipt = session.get(receipt_model, key)  # type: ignore[attr-defined]
        if receipt is None:
            return None
        if receipt.tenant_id != tenant_id:
            raise ValueError("econ erasure idempotency key tenant mismatch")
        return int(receipt.deleted_count)

    def _delete_sync(
        self,
        tenant_id: str,
        keys: list[str],
        idempotency_key: str,
    ) -> int:
        # Lazy import: keeps the econ_plane dependency out of the base install.
        from sqlalchemy import delete
        from sqlalchemy.exc import IntegrityError

        session_factory = self._session_factory
        if session_factory is None:
            from zeroth.econ.plane.database import SessionLocal

            session_factory = SessionLocal
        from zeroth.econ.plane.instrumentation.models import (
            EconErasureReceipt,
            ExecutionEvent,
            OutcomeEvent,
        )

        deleted = 0
        session = session_factory()
        try:
            replay = self._replay(session, EconErasureReceipt, tenant_id, idempotency_key)
            if replay is not None:
                return replay

            # Claim the idempotency key BEFORE deleting anything.
            #
            # The read above and the delete below are one transaction already --
            # session.close() rolls back anything pending -- so this is not a
            # partial-write fix.  The defect is a race: two callers holding the
            # same key both read None here, both delete, and both insert.  Only
            # one can win the primary key, and the loser's row deletions are
            # counted into a receipt that never commits.
            #
            # SELECT ... FOR UPDATE cannot fix that: with no receipt row yet there
            # is nothing to lock.  Inserting first makes the primary-key index
            # itself the lock -- a concurrent claimant blocks on this uncommitted
            # row and, once we commit, loses cleanly and replays our count.
            receipt = EconErasureReceipt(
                operation_id=idempotency_key,
                tenant_id=tenant_id,
                deleted_count=0,
                created_at=datetime.now(UTC),
            )
            session.add(receipt)
            try:
                session.flush()
            except IntegrityError:
                # Lost the claim: the winner has committed (an uncommitted rival
                # would have blocked us instead of colliding). Replay its count
                # and delete nothing -- our own transaction dies in the rollback.
                session.rollback()
                replay = self._replay(session, EconErasureReceipt, tenant_id, idempotency_key)
                if replay is None:
                    raise
                return replay

            for model in (ExecutionEvent, OutcomeEvent):
                result = session.execute(
                    delete(model).where(
                        model.tenant_id == tenant_id,
                        model.join_key.in_(keys),
                    )
                )
                deleted += int(result.rowcount or 0)
            receipt.deleted_count = deleted
            session.commit()
        finally:
            session.close()
        return deleted
