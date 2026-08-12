"""Executable isolation probes owned by the production audit surface."""

from __future__ import annotations

from zeroth.governance.audit.models import NodeAuditRecord
from zeroth.governance.audit.repository import AuditRepository
from zeroth.platform.storage import AsyncDatabase, NullWorkspaceScopeContext, ResourceOperation


def _scope(tenant_id: str) -> NullWorkspaceScopeContext:
    """Resolve scope for structurally scoped persistence."""
    return NullWorkspaceScopeContext(tenant_id=tenant_id)


def _audit(tenant: str, audit_id: str = "driver-audit") -> NodeAuditRecord:
    """Resolve audit for structurally scoped persistence."""
    return NodeAuditRecord(
        audit_id=audit_id,
        run_id="driver-run",
        node_id="driver-node",
        graph_version_ref="driver-graph",
        deployment_ref="driver-deployment",
        tenant_id=tenant,
        status="completed",
    )


async def _drive_audit_resource(
    database: AsyncDatabase, operation: ResourceOperation, *, chain: bool
) -> None:
    """Exercise audit resource operations through the tenant-isolation matrix."""
    owner = AuditRepository.scoped(database, _scope("driver-owner"))
    foreign = AuditRepository.scoped(database, _scope("driver-foreign"))
    owner_written = await owner.write(_audit("driver-owner"))
    if operation is ResourceOperation.CREATE:
        await foreign.write(_audit("driver-foreign", "driver-foreign-audit"))
    elif operation is ResourceOperation.READ:
        if chain:
            # ``write`` reads the current chain head before appending.  Using
            # the same run makes an unscoped head read observable in the
            # returned sequence/digest without exposing a raw table adapter.
            foreign_written = await foreign.write(_audit("driver-foreign", "driver-foreign-audit"))
            assert foreign_written.chain_sequence == 1
            assert foreign_written.previous_record_digest is None
            assert owner_written.chain_sequence == 1
        else:
            assert await foreign.get("driver-audit") is None
            assert await foreign.get("unknown-audit") is None
    elif operation is ResourceOperation.ENUMERATE:
        assert await foreign.list() == []
    elif chain:
        await foreign.write(_audit("driver-foreign", "driver-foreign-audit"))
        if operation is ResourceOperation.UPDATE:
            await foreign.write(_audit("driver-foreign", "driver-foreign-audit-2"))
    else:
        assert await foreign.crypto_erase("driver-audit", reason="foreign") is None
    assert await owner.get("driver-audit") is not None


async def _drive_audit_chain_heads(database: AsyncDatabase, operation: ResourceOperation) -> None:
    """Exercise audit chain heads operations through the tenant-isolation matrix."""
    await _drive_audit_resource(database, operation, chain=True)


async def _drive_node_audits(database: AsyncDatabase, operation: ResourceOperation) -> None:
    """Exercise node audits operations through the tenant-isolation matrix."""
    await _drive_audit_resource(database, operation, chain=False)
