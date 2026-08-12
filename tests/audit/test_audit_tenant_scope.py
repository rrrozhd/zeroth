from __future__ import annotations

from datetime import UTC, datetime

import pytest

from zeroth.governance.audit import AuditContinuityVerifier, AuditRepository, NodeAuditRecord
from zeroth.platform.storage import ScopeContext


def _scope(tenant_id: str, workspace_id: str) -> ScopeContext:
    return ScopeContext(tenant_id=tenant_id, workspace_id=workspace_id)


def _record(*, audit_id: str, run_id: str, tenant_id: str, workspace_id: str) -> NodeAuditRecord:
    now = datetime(2026, 8, 11, tzinfo=UTC)
    return NodeAuditRecord(
        audit_id=audit_id,
        run_id=run_id,
        thread_id="shared-thread",
        node_id="node",
        graph_version_ref="graph:v1",
        deployment_ref="deployment:v1",
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        status="completed",
        started_at=now,
        completed_at=now,
    )


async def test_same_run_id_has_independent_valid_chain_per_tenant(sqlite_db) -> None:
    tenant_a = AuditRepository.scoped(sqlite_db, _scope("tenant-a", "workspace-a"))
    tenant_b = AuditRepository.scoped(sqlite_db, _scope("tenant-b", "workspace-b"))

    a1 = await tenant_a.write(
        _record(
            audit_id="tenant-a:first",
            run_id="shared-run",
            tenant_id="tenant-a",
            workspace_id="workspace-a",
        )
    )
    b1 = await tenant_b.write(
        _record(
            audit_id="tenant-b:first",
            run_id="shared-run",
            tenant_id="tenant-b",
            workspace_id="workspace-b",
        )
    )
    a2 = await tenant_a.write(
        _record(
            audit_id="tenant-a:second",
            run_id="shared-run",
            tenant_id="tenant-a",
            workspace_id="workspace-a",
        )
    )
    b2 = await tenant_b.write(
        _record(
            audit_id="tenant-b:second",
            run_id="shared-run",
            tenant_id="tenant-b",
            workspace_id="workspace-b",
        )
    )

    assert [a1.chain_sequence, a2.chain_sequence] == [1, 2]
    assert [b1.chain_sequence, b2.chain_sequence] == [1, 2]
    assert a1.previous_record_digest is None
    assert b1.previous_record_digest is None
    assert a2.previous_record_digest == a1.record_digest
    assert b2.previous_record_digest == b1.record_digest
    assert [item.audit_id for item in await tenant_a.list_by_run("shared-run")] == [
        "tenant-a:first",
        "tenant-a:second",
    ]
    assert [item.audit_id for item in await tenant_b.list_by_run("shared-run")] == [
        "tenant-b:first",
        "tenant-b:second",
    ]
    assert (await AuditContinuityVerifier(tenant_a).verify_run("shared-run")).verified is True
    assert (await AuditContinuityVerifier(tenant_b).verify_run("shared-run")).verified is True


@pytest.mark.parametrize(
    ("tenant_id", "workspace_id", "match"),
    [
        ("tenant-b", "workspace-a", "tenant_id does not match bound scope"),
        ("tenant-a", "workspace-b", "workspace_id does not match bound scope"),
    ],
)
async def test_bound_repository_rejects_record_owner_mismatch(
    sqlite_db,
    tenant_id: str,
    workspace_id: str,
    match: str,
) -> None:
    repository = AuditRepository.scoped(sqlite_db, _scope("tenant-a", "workspace-a"))
    record = _record(
        audit_id=f"mismatch:{tenant_id}:{workspace_id}",
        run_id="run",
        tenant_id=tenant_id,
        workspace_id=workspace_id,
    )

    with pytest.raises(ValueError, match=match):
        await repository.write(record)


async def test_query_owner_cannot_override_bound_repository_scope(sqlite_db) -> None:
    repository = AuditRepository.scoped(sqlite_db, _scope("tenant-a", "workspace-a"))

    with pytest.raises(ValueError, match="tenant_id does not match bound scope"):
        await repository.list_by_run("run", tenant_id="tenant-b")
    with pytest.raises(ValueError, match="workspace_id does not match bound scope"):
        await repository.list_by_run("run", workspace_id="workspace-b", workspace_scoped=True)
