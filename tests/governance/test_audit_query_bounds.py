"""Audit reads can be bounded at the call site (ZER-48 / A02-14).

``AuditRepository.list`` had no bound at all, so the econ-analytics and
rightsizing routes fetched every audit record the deployment had ever written
and then filtered in Python.  The runs half of the same handler was already
capped (``Query(default=200, ge=1, le=1000)``); only the audit half was not.

A bound on a time-ordered read has to mean *the most recent N*, not *the oldest
N* — a limit applied to the ascending order would have returned the deployment's
first page of history forever.  That is what these tests pin.
"""

from __future__ import annotations

import pytest

from zeroth.governance.audit import AuditQuery, AuditRepository
from zeroth.governance.audit.models import NodeAuditRecord
from zeroth.platform.storage import NullWorkspaceScopeContext
from zeroth.platform.storage.async_sqlite import AsyncSQLiteDatabase


def _repository(tmp_path) -> tuple[AuditRepository, AsyncSQLiteDatabase]:  # noqa: ANN001
    from zeroth.service.bootstrap import run_migrations

    db_path = str(tmp_path / "audit.db")
    run_migrations(f"sqlite:///{db_path}")
    database = AsyncSQLiteDatabase(path=db_path)
    return AuditRepository(database, NullWorkspaceScopeContext(tenant_id="tenant-a")), database


def _record(index: int) -> NodeAuditRecord:
    return NodeAuditRecord(
        audit_id=f"audit-{index:03d}",
        run_id=f"run-{index:03d}",
        node_id="node-a",
        graph_version_ref="graph-v1",
        deployment_ref="deployment-a",
        tenant_id="tenant-a",
        status="completed",
    )


@pytest.mark.asyncio
async def test_list_without_limit_returns_everything(tmp_path) -> None:  # noqa: ANN001
    repository, database = _repository(tmp_path)
    try:
        for index in range(5):
            await repository.write(_record(index))

        records = await repository.list(AuditQuery(deployment_ref="deployment-a"))

        assert len(records) == 5
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_limit_returns_the_most_recent_records(tmp_path) -> None:  # noqa: ANN001
    repository, database = _repository(tmp_path)
    try:
        for index in range(10):
            await repository.write(_record(index))

        records = await repository.list(AuditQuery(deployment_ref="deployment-a"), limit=3)

        assert [r.audit_id for r in records] == ["audit-007", "audit-008", "audit-009"], (
            "a bounded audit read must return the newest rows, in time order"
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_limit_rejects_a_negative_bound(tmp_path) -> None:  # noqa: ANN001
    repository, database = _repository(tmp_path)
    try:
        with pytest.raises(ValueError):
            await repository.list(AuditQuery(), limit=-1)
    finally:
        await database.close()
