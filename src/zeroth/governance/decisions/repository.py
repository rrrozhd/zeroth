"""Tenant-scoped, idempotent storage for tool-enforcement decisions.

**The unique index is the authority, not a pre-flight read.** ``decision_records``
carries ``UNIQUE (tenant_id, idempotency_key)`` (migration 016), and
:meth:`DecisionRepository.record` inserts against it with ``ON CONFLICT DO
NOTHING`` and then re-reads the row, rather than checking whether the key exists
and inserting if it did not. A check-then-insert leaves a window in which two
concurrent replays of one key both find nothing and both evaluate; the pattern
here closes it the same way
:class:`~zeroth.governance.guardrails.rate_limit.TokenBucketRateLimiter` does,
and stays portable across SQLite and PostgreSQL instead of catching a
driver-specific integrity error.

**The digest comparison is on both branches, and is one function.** Whether a key
is found by the pre-check (:meth:`~DecisionRepository.find_by_idempotency_key`)
or by the re-read after a losing insert, the stored ``request_digest`` is
compared against the incoming one by :func:`_ensure_digest_matches`. Returning
the re-read row without that comparison would hand a concurrent
same-key/different-action caller the *other* action's verdict, which is the
idempotency contract failing open. Sharing one function is what stops the two
branches drifting apart.

**Tenancy is part of the key, never a filter applied afterwards.** Every
statement names ``tenant_id`` alongside ``idempotency_key``, so the same key
from two tenants is two records and a lookup cannot cross the boundary.
"""

from __future__ import annotations

from datetime import datetime
from hmac import compare_digest
from typing import Any
from uuid import uuid4

from zeroth.governance.decisions.request import (
    DecisionKind,
    DecisionRequest,
    DecisionResponse,
    DecisionVerdict,
    StoredDecision,
)
from zeroth.platform.primitives import utc_now
from zeroth.platform.storage import (
    SERVICE_SCOPE_REGISTRY,
    AsyncDatabase,
    NullWorkspaceScopeContext,
    ResourceOperation,
    ScopedTable,
)
from zeroth.platform.storage.scoping import (
    named_isolation_probe,
    persistence_operation,
    persistence_surface,
)

_COLUMNS = (
    "decision_id, tenant_id, idempotency_key, request_digest, decision_kind, "
    "reason_code, approval_ref, policy_version, deployment_ref, principal_id, "
    "action_name, action_fingerprint, created_at"
)


class IdempotencyConflictError(ValueError):
    """An idempotency key was reused for a materially different request.

    Subclasses :class:`ValueError` so a caller that does not know about the
    decision layer still treats it as bad input rather than as a server fault:
    the transport layer maps it to a conflict response, never to a retry.
    """


def _for_update(backend: str) -> str:
    """Row-lock suffix for the read side of an insert-then-re-read.

    ``write_lock=True`` gives SQLite an exclusive ``BEGIN IMMEDIATE``, but on
    PostgreSQL it only sets ``lock_timeout``; the row itself must be locked with
    ``SELECT ... FOR UPDATE``.

    Args:
        backend: The database backend name.

    Returns:
        The clause to append to the read, empty on SQLite.
    """
    return " FOR UPDATE" if backend == "postgres" else ""


def _ensure_digest_matches(stored: StoredDecision, digest: str, request: DecisionRequest) -> None:
    """Raise unless the stored decision was made for this exact request.

    The single place the idempotency contract is decided, called from both the
    pre-check and the post-insert re-read so neither can quietly relax it.
    Compared with :func:`hmac.compare_digest` because the two digests are
    attacker-influenced material being checked for equality.

    Args:
        stored: The decision already recorded under the key.
        digest: The digest of the incoming request.
        request: The incoming request, named only for the error message.

    Raises:
        IdempotencyConflictError: If the stored request digest differs.
    """
    if compare_digest(stored.request_digest, digest):
        return
    raise IdempotencyConflictError(
        f"idempotency key {request.idempotency_key!r} for tenant "
        f"{request.tenant_id!r} was already used for a different request"
    )


def _row_to_stored(row: dict[str, Any]) -> StoredDecision:
    """Rebuild a stored decision from one ``decision_records`` row."""
    response = DecisionResponse(
        decision_id=str(row["decision_id"]),
        kind=DecisionKind(str(row["decision_kind"])),
        reason_code=str(row["reason_code"]),
        approval_ref=None if row["approval_ref"] is None else str(row["approval_ref"]),
        policy_version=str(row["policy_version"]),
        tenant_id=str(row["tenant_id"]),
        issued_at=datetime.fromisoformat(str(row["created_at"])),
    )
    return StoredDecision(response=response, request_digest=str(row["request_digest"]))


@persistence_surface(
    "service.decision_records", probe=named_isolation_probe("_drive_decision_records")
)
class DecisionRepository:
    """Read and append tool-enforcement decisions, keyed per tenant."""

    def __init__(self, database: AsyncDatabase) -> None:
        self._database = database

    def _decisions(self, tenant_id: str) -> ScopedTable:
        context = (
            NullWorkspaceScopeContext.for_default_compatibility()
            if tenant_id == "default"
            else NullWorkspaceScopeContext(tenant_id=tenant_id)
        )
        return ScopedTable(
            self._database,
            SERVICE_SCOPE_REGISTRY,
            "service.decision_records",
            context,
        )

    @persistence_operation(ResourceOperation.READ)
    async def find_by_idempotency_key(
        self,
        tenant_id: str,
        idempotency_key: str,
    ) -> StoredDecision | None:
        """Load the decision recorded under a key for one tenant.

        Args:
            tenant_id: The tenant the key is scoped to.
            idempotency_key: The caller's replay token.

        Returns:
            The stored decision and the digest it was made for, or ``None``
            when this tenant has not used the key.
        """
        row = await self._decisions(tenant_id).select_one(
            where={"idempotency_key": idempotency_key},
            columns=tuple(column.strip() for column in _COLUMNS.split(",")),
        )
        return None if row is None else _row_to_stored(row)

    @persistence_operation(ResourceOperation.READ)
    async def find_replay(
        self,
        request: DecisionRequest,
        digest: str,
    ) -> DecisionResponse | None:
        """Re-serve the decision already recorded for this exact request.

        The read-side half of the idempotency contract, sharing
        :func:`_ensure_digest_matches` with :meth:`record` so a caller cannot
        reach a stored decision without the digest comparison happening.

        Args:
            request: The incoming request.
            digest: That request's digest.

        Returns:
            The original decision when the key was already used for this same
            request, or ``None`` when the key is unused.

        Raises:
            IdempotencyConflictError: If the key already carries a decision made
                for a different request.
        """
        stored = await self.find_by_idempotency_key(
            request.tenant_id,
            request.idempotency_key,
        )
        if stored is None:
            return None
        _ensure_digest_matches(stored, digest, request)
        return stored.response

    @persistence_operation(ResourceOperation.CREATE, ResourceOperation.READ)
    async def record(
        self,
        request: DecisionRequest,
        *,
        digest: str,
        verdict: DecisionVerdict,
    ) -> DecisionResponse:
        """Append a decision, or re-serve the one already stored under its key.

        Args:
            request: The request the verdict was reached for.
            digest: That request's digest, as
                :func:`~zeroth.governance.decisions.request.request_digest`
                computes it.
            verdict: The decided outcome to record.

        Returns:
            The recorded decision -- the original one when the key was already
            used for this same request, so a replay never mints a second
            identity.

        Raises:
            IdempotencyConflictError: If the key already carries a decision made
                for a different request.
        """
        candidate = DecisionResponse(
            decision_id=uuid4().hex,
            kind=verdict.kind,
            reason_code=verdict.reason_code,
            approval_ref=verdict.approval_ref,
            policy_version=verdict.policy_version,
            tenant_id=request.tenant_id,
            issued_at=utc_now(),
        )
        row = await self._insert_then_read(request, digest=digest, candidate=candidate)
        if row is None:  # pragma: no cover - the row was just written or already there.
            raise IdempotencyConflictError(
                f"decision for idempotency key {request.idempotency_key!r} vanished"
            )
        stored = _row_to_stored(row)
        _ensure_digest_matches(stored, digest, request)
        return stored.response

    async def _insert_then_read(
        self,
        request: DecisionRequest,
        *,
        digest: str,
        candidate: DecisionResponse,
    ) -> dict[str, Any] | None:
        """Insert the candidate unless the key is taken, then read whichever row won.

        One write-locked transaction, so the insert and the read cannot be
        separated by a competing writer. ``ON CONFLICT DO NOTHING`` makes the
        losing insert a no-op instead of an integrity error, which keeps the
        statement portable across SQLite and PostgreSQL.
        """
        async with self._decisions(request.tenant_id).transaction(write_lock=True) as decisions:
            await decisions.insert_if_absent(
                {
                    "decision_id": candidate.decision_id,
                    "idempotency_key": request.idempotency_key,
                    "request_digest": digest,
                    "decision_kind": candidate.kind.value,
                    "reason_code": candidate.reason_code,
                    "approval_ref": candidate.approval_ref,
                    "policy_version": candidate.policy_version,
                    "deployment_ref": request.deployment_ref,
                    "principal_id": request.principal_id,
                    "action_name": request.action.name,
                    "action_fingerprint": request.action.fingerprint,
                    "created_at": candidate.issued_at.isoformat(),
                },
                conflict_columns=("tenant_id", "idempotency_key"),
            )
            return await decisions.select_one(
                where={"idempotency_key": request.idempotency_key},
                columns=tuple(column.strip() for column in _COLUMNS.split(",")),
                for_update=True,
            )


__all__ = ["DecisionRepository", "IdempotencyConflictError"]
