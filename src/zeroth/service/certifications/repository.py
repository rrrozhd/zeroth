"""Tenant/workspace-scoped certification persistence and atomic promotion."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from zeroth.platform.storage import (
    SERVICE_SCOPE_REGISTRY,
    AsyncDatabase,
    NullWorkspaceScopeContext,
    ResourceOperation,
    ScopeContext,
    ScopedTable,
)
from zeroth.platform.storage.json import load_typed_value, to_json_value
from zeroth.platform.storage.scoping import (
    named_isolation_probe,
    persistence_operation,
    persistence_surface,
)
from zeroth.service.certifications.models import (
    AppCertification,
    CertificationEvent,
    CertificationOverride,
    CertificationState,
    OverrideScope,
    PromotionConflictError,
    PromotionRejectedError,
)
from zeroth.service.certifications.receipt import SignedPromotionReceipt


def _scope(tenant_id: str, workspace_id: str | None):
    if workspace_id is None:
        return (
            NullWorkspaceScopeContext.for_default_compatibility()
            if tenant_id == "default"
            else NullWorkspaceScopeContext(tenant_id=tenant_id)
        )
    return (
        ScopeContext.for_default_compatibility(workspace_id=workspace_id)
        if tenant_id == "default"
        else ScopeContext(tenant_id=tenant_id, workspace_id=workspace_id)
    )


@persistence_surface(
    "service.app_certifications", probe=named_isolation_probe("_drive_app_certifications")
)
@persistence_surface(
    "service.app_certification_events",
    probe=named_isolation_probe("_drive_app_certification_events"),
    # One class owns two resources, so the events surface has to say which
    # methods are its own. Without this it inherits grant_override's UPDATE, and
    # the events table is append-only -- its registry definition is
    # {CREATE, READ, ENUMERATE} -- so the surface declared an operation the
    # resource does not have.
    method_names=frozenset({"create", "events"}),
)
class CertificationRepository:
    """Persist certification state with an append-only event stream."""

    def __init__(self, database: AsyncDatabase):
        self._database = database

    def _table(
        self, resource: str, tenant_id: str, workspace_id: str | None
    ) -> ScopedTable:
        return ScopedTable(
            self._database,
            SERVICE_SCOPE_REGISTRY,
            resource,
            _scope(tenant_id, workspace_id),
        )

    def _certifications(self, tenant_id: str, workspace_id: str | None) -> ScopedTable:
        return self._table("service.app_certifications", tenant_id, workspace_id)

    def _events(self, tenant_id: str, workspace_id: str | None) -> ScopedTable:
        return self._table("service.app_certification_events", tenant_id, workspace_id)

    @persistence_operation(ResourceOperation.CREATE)
    async def create(self, record: AppCertification, *, actor_id: str) -> AppCertification:
        """Create a certification and initial event atomically."""
        certifications = self._certifications(record.tenant_id, record.workspace_id)
        events = self._events(record.tenant_id, record.workspace_id)
        async with certifications.transaction(write_lock=True) as transaction:
            await transaction.insert(self._record_values(record))
            await events.in_transaction(transaction).insert(
                self._event_values(
                    record,
                    event_type="registered",
                    actor_id=actor_id,
                    at=record.created_at,
                )
            )
        loaded = await self.get(record.certification_id, record.tenant_id, record.workspace_id)
        assert loaded is not None
        return loaded

    @persistence_operation(ResourceOperation.READ)
    async def get(
        self, certification_id: str, tenant_id: str, workspace_id: str | None
    ) -> AppCertification | None:
        """Load one certification inside its owner scope."""
        async with self._certifications(tenant_id, workspace_id).transaction() as table:
            row = await table.select_one(where={"certification_id": certification_id})
        return None if row is None else self._hydrate(row)

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def list(
        self, tenant_id: str, workspace_id: str | None
    ) -> list[AppCertification]:
        """List newest certifications in one scope."""
        async with self._certifications(tenant_id, workspace_id).transaction() as table:
            rows = await table.select(order_by_desc=("created_at",))
        return [self._hydrate(row) for row in rows]

    @persistence_operation(ResourceOperation.READ)
    async def get_by_target(
        self, target_key: str, tenant_id: str, workspace_id: str | None
    ) -> AppCertification | None:
        """Load the certification owning a production target."""
        async with self._certifications(tenant_id, workspace_id).transaction() as table:
            row = await table.select_one(where={"promotion_target_key": target_key})
        return None if row is None else self._hydrate(row)

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def events(
        self, certification_id: str, tenant_id: str, workspace_id: str | None
    ) -> list[CertificationEvent]:
        """Return one immutable certification timeline."""
        async with self._events(tenant_id, workspace_id).transaction() as table:
            rows = await table.select(
                where={"certification_id": certification_id},
                order_by=("created_at", "event_id"),
            )
        return [self._hydrate_event(row) for row in rows]

    @persistence_operation(
        ResourceOperation.READ, ResourceOperation.UPDATE, ResourceOperation.CREATE
    )
    async def grant_override(
        self,
        certification_id: str,
        tenant_id: str,
        workspace_id: str | None,
        override: CertificationOverride,
        *,
        actor_id: str,
        at: datetime,
    ) -> AppCertification:
        """Persist an override and event in the same transaction."""
        certifications = self._certifications(tenant_id, workspace_id)
        events = self._events(tenant_id, workspace_id)
        async with certifications.transaction(write_lock=True) as transaction:
            row = await transaction.select_one(
                where={"certification_id": certification_id}, for_update=True
            )
            if row is None:
                raise KeyError(certification_id)
            record = self._hydrate(row)
            if record.state is CertificationState.REVOKED:
                raise PromotionRejectedError("revoked certification cannot be overridden")
            await transaction.update(
                {"override_json": to_json_value(override), "updated_at": at.isoformat()},
                where={"certification_id": certification_id},
            )
            updated = record.model_copy(update={"override": override, "updated_at": at})
            await events.in_transaction(transaction).insert(
                self._event_values(
                    updated,
                    event_type="override_granted",
                    actor_id=actor_id,
                    at=at,
                    reason=override.reason,
                    scopes=override.scopes,
                )
            )
        loaded = await self.get(certification_id, tenant_id, workspace_id)
        assert loaded is not None
        return loaded

    @persistence_operation(
        ResourceOperation.READ, ResourceOperation.UPDATE, ResourceOperation.CREATE
    )
    async def revoke(
        self,
        certification_id: str,
        tenant_id: str,
        workspace_id: str | None,
        *,
        reason: str,
        actor_id: str,
        at: datetime,
    ) -> AppCertification:
        """Revoke a record and release its target atomically."""
        certifications = self._certifications(tenant_id, workspace_id)
        events = self._events(tenant_id, workspace_id)
        async with certifications.transaction(write_lock=True) as transaction:
            row = await transaction.select_one(
                where={"certification_id": certification_id}, for_update=True
            )
            if row is None:
                raise KeyError(certification_id)
            record = self._hydrate(row)
            if record.state is CertificationState.REVOKED:
                return record
            update = {
                "state": CertificationState.REVOKED.value,
                "promotion_target_key": None,
                "revoked_at": at.isoformat(),
                "revocation_reason": reason,
                "updated_at": at.isoformat(),
            }
            await transaction.update(update, where={"certification_id": certification_id})
            updated = record.model_copy(
                update={
                    "state": CertificationState.REVOKED,
                    "promotion_target_key": None,
                    "revoked_at": at,
                    "revocation_reason": reason,
                    "updated_at": at,
                }
            )
            await events.in_transaction(transaction).insert(
                self._event_values(
                    updated,
                    event_type="revoked",
                    actor_id=actor_id,
                    at=at,
                    reason=reason,
                )
            )
        loaded = await self.get(certification_id, tenant_id, workspace_id)
        assert loaded is not None
        return loaded

    @persistence_operation(
        ResourceOperation.READ, ResourceOperation.UPDATE, ResourceOperation.CREATE
    )
    async def promote(
        self,
        certification_id: str,
        tenant_id: str,
        workspace_id: str | None,
        *,
        target_key: str,
        actor_id: str,
        at: datetime,
    ) -> AppCertification:
        """Claim a production target exactly once with the promotion event."""
        certifications = self._certifications(tenant_id, workspace_id)
        events = self._events(tenant_id, workspace_id)
        try:
            async with certifications.transaction(write_lock=True) as transaction:
                row = await transaction.select_one(
                    where={"certification_id": certification_id}, for_update=True
                )
                if row is None:
                    raise KeyError(certification_id)
                record = self._hydrate(row)
                if record.state is CertificationState.REVOKED:
                    raise PromotionRejectedError("certification is revoked")
                if record.state is CertificationState.PROMOTED:
                    if record.promotion_target_key == target_key:
                        return record
                    raise PromotionConflictError("certification already owns another target")
                owner = await transaction.select_one(
                    where={"promotion_target_key": target_key}, for_update=True
                )
                if owner is not None:
                    raise PromotionConflictError("production target is already promoted")
                await transaction.update(
                    {
                        "state": CertificationState.PROMOTED.value,
                        "promotion_target_key": target_key,
                        "promoted_at": at.isoformat(),
                        "updated_at": at.isoformat(),
                    },
                    where={"certification_id": certification_id},
                )
                promoted = record.model_copy(
                    update={
                        "state": CertificationState.PROMOTED,
                        "promotion_target_key": target_key,
                        "promoted_at": at,
                        "updated_at": at,
                    }
                )
                await events.in_transaction(transaction).insert(
                    self._event_values(
                        promoted, event_type="promoted", actor_id=actor_id, at=at
                    )
                )
        except PromotionConflictError:
            raise
        except Exception as exc:
            if "unique" in str(exc).lower() or "duplicate key" in str(exc).lower():
                raise PromotionConflictError(
                    "production target is already promoted"
                ) from exc
            raise
        loaded = await self.get(certification_id, tenant_id, workspace_id)
        assert loaded is not None
        return loaded

    @staticmethod
    def _record_values(record: AppCertification) -> dict[str, object]:
        """Serialize a certification for structured storage."""
        return {
            "row_id": uuid4().hex,
            "certification_id": record.certification_id,
            "receipt_json": to_json_value(record.receipt),
            "receipt_digest": record.receipt.digest,
            "state": record.state.value,
            "promotion_target_key": record.promotion_target_key,
            "promoted_at": record.promoted_at.isoformat() if record.promoted_at else None,
            "revoked_at": record.revoked_at.isoformat() if record.revoked_at else None,
            "revocation_reason": record.revocation_reason,
            "override_json": to_json_value(record.override) if record.override else None,
            "created_at": record.created_at.isoformat(),
            "updated_at": record.updated_at.isoformat(),
        }

    @staticmethod
    def _event_values(
        record: AppCertification,
        *,
        event_type: str,
        actor_id: str,
        at: datetime,
        reason: str | None = None,
        scopes: tuple[OverrideScope, ...] = (),
    ) -> dict[str, object]:
        """Serialize one append-only event."""
        return {
            "event_id": uuid4().hex,
            "certification_id": record.certification_id,
            "event_type": event_type,
            "state": record.state.value,
            "promotion_target_key": record.promotion_target_key,
            "actor_id": actor_id,
            "reason": reason,
            "scopes_json": to_json_value(tuple(scope.value for scope in scopes)),
            "override_expires_at": (
                record.override.expires_at.isoformat() if record.override else None
            ),
            "created_at": at.isoformat(),
        }

    @staticmethod
    def _hydrate(row: dict[str, object]) -> AppCertification:
        override_json = row["override_json"]
        return AppCertification(
            certification_id=str(row["certification_id"]),
            tenant_id=str(row["tenant_id"]),
            workspace_id=str(row["workspace_id"]) if row["workspace_id"] else None,
            receipt=load_typed_value(row["receipt_json"], SignedPromotionReceipt),
            state=CertificationState(str(row["state"])),
            promotion_target_key=(
                str(row["promotion_target_key"]) if row["promotion_target_key"] else None
            ),
            promoted_at=(
                datetime.fromisoformat(str(row["promoted_at"])) if row["promoted_at"] else None
            ),
            revoked_at=(
                datetime.fromisoformat(str(row["revoked_at"])) if row["revoked_at"] else None
            ),
            revocation_reason=(
                str(row["revocation_reason"]) if row["revocation_reason"] else None
            ),
            override=(
                load_typed_value(override_json, CertificationOverride)
                if override_json
                else None
            ),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    @staticmethod
    def _hydrate_event(row: dict[str, object]) -> CertificationEvent:
        scopes = load_typed_value(row["scopes_json"], tuple[str, ...])
        return CertificationEvent(
            event_id=str(row["event_id"]),
            certification_id=str(row["certification_id"]),
            tenant_id=str(row["tenant_id"]),
            workspace_id=str(row["workspace_id"]) if row["workspace_id"] else None,
            event_type=str(row["event_type"]),
            state=CertificationState(str(row["state"])),
            promotion_target_key=(
                str(row["promotion_target_key"]) if row["promotion_target_key"] else None
            ),
            actor_id=str(row["actor_id"]),
            reason=str(row["reason"]) if row["reason"] else None,
            scopes=tuple(OverrideScope(value) for value in scopes),
            override_expires_at=(
                datetime.fromisoformat(str(row["override_expires_at"]))
                if row["override_expires_at"]
                else None
            ),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )


__all__ = ["CertificationRepository"]
