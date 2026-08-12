from __future__ import annotations

from datetime import UTC, datetime

import pytest

from zeroth.governance.audit import AuditContinuityVerifier, AuditRepository, NodeAuditRecord
from zeroth.platform.storage import (
    NullWorkspaceScopeContext,
    ScopeContext,
    TenantWideScopeContext,
)


def _scope(tenant_id: str, workspace_id: str) -> ScopeContext:
    return ScopeContext(tenant_id=tenant_id, workspace_id=workspace_id)


def _record(
    *, audit_id: str, run_id: str, tenant_id: str, workspace_id: str | None
) -> NodeAuditRecord:
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


async def test_null_workspace_repository_never_touches_same_tenant_workspace_row(sqlite_db) -> None:
    null_repository = AuditRepository.scoped(
        sqlite_db, NullWorkspaceScopeContext(tenant_id="tenant-a")
    )
    workspace_repository = AuditRepository.scoped(
        sqlite_db, ScopeContext(tenant_id="tenant-a", workspace_id="workspace-a")
    )
    tenant_wide_repository = AuditRepository.scoped(
        sqlite_db, TenantWideScopeContext(tenant_id="tenant-a")
    )
    null_record = _record(
        audit_id="null-audit",
        run_id="null-run",
        tenant_id="tenant-a",
        workspace_id=None,
    )
    workspace_record = _record(
        audit_id="workspace-audit",
        run_id="workspace-run",
        tenant_id="tenant-a",
        workspace_id="workspace-a",
    )
    await null_repository.write(null_record)
    await workspace_repository.write(workspace_record)

    assert await null_repository.get("workspace-audit") is None
    assert [record.audit_id for record in await null_repository.list()] == ["null-audit"]
    assert {record.audit_id for record in await tenant_wide_repository.list()} == {
        "null-audit",
        "workspace-audit",
    }

    await null_repository.crypto_erase("null-audit", reason="canary")
    assert (await null_repository.get("null-audit")).erased is True  # type: ignore[union-attr]
    assert (await workspace_repository.get("workspace-audit")).erased is False  # type: ignore[union-attr]

    await null_repository._audits.update(  # noqa: SLF001
        {"record_json": "{}"}, where={"audit_id": "workspace-audit"}
    )
    assert (await workspace_repository.get("workspace-audit")).audit_id == "workspace-audit"  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="workspace-scoped creates require a workspace context"):
        await tenant_wide_repository.write(
            _record(
                audit_id="privileged-write",
                run_id="privileged-run",
                tenant_id="tenant-a",
                workspace_id=None,
            )
        )
