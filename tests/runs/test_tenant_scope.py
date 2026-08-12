from __future__ import annotations

import inspect

import pytest

from zeroth.integrations.persistence.runs import RunRepository, ThreadRepository
from zeroth.platform.storage import ScopeContext
from zeroth.platform.storage.scoped_table import BoundStructuredTable
from zeroth.runtime.runs import Run, Thread


@pytest.mark.parametrize("repository_type", [RunRepository, ThreadRepository])
def test_run_repository_constructors_require_scope_context(repository_type: type) -> None:
    parameters = inspect.signature(repository_type).parameters

    assert "scope_context" in parameters
    assert parameters["scope_context"].default is inspect.Parameter.empty


@pytest.mark.parametrize(
    ("repository_type", "method_name"),
    [
        (RunRepository, "get"),
        (RunRepository, "get_checkpoint"),
        (RunRepository, "set_active_run_id"),
        (ThreadRepository, "get"),
        (ThreadRepository, "list"),
        (ThreadRepository, "attach_run"),
        (ThreadRepository, "get_active_run_id"),
        (ThreadRepository, "get_latest_run_id"),
        (ThreadRepository, "list_run_ids"),
        (ThreadRepository, "set_active_run_id"),
    ],
)
def test_run_public_scope_is_constructor_bound(repository_type: type, method_name: str) -> None:
    parameters = inspect.signature(getattr(repository_type, method_name)).parameters

    assert "tenant_id" not in parameters
    assert "workspace_id" not in parameters


async def test_run_identifier_collision_preserves_each_scope_owner(runs_db) -> None:
    scope_a = ScopeContext(tenant_id="tenant-a", workspace_id="workspace-a")
    scope_b = ScopeContext(tenant_id="tenant-b", workspace_id="workspace-b")
    repository_a = RunRepository(runs_db, scope_a)
    repository_b = RunRepository(runs_db, scope_b)
    shared = "shared-run"

    await repository_a.create(
        Run(
            run_id=shared,
            thread_id="shared-thread",
            graph_version_ref="graph:a",
            deployment_ref="deployment:a",
            tenant_id=scope_a.tenant_id,
            workspace_id=scope_a.workspace_id,
        )
    )
    await repository_b.create(
        Run(
            run_id=shared,
            thread_id="shared-thread",
            graph_version_ref="graph:b",
            deployment_ref="deployment:b",
            tenant_id=scope_b.tenant_id,
            workspace_id=scope_b.workspace_id,
        )
    )

    assert (await repository_a.get(shared)).deployment_ref == "deployment:a"
    assert (await repository_b.get(shared)).deployment_ref == "deployment:b"


async def test_foreign_thread_operations_match_unknown_scope(runs_db) -> None:
    owner = ThreadRepository(
        runs_db, ScopeContext(tenant_id="tenant-a", workspace_id="workspace-a")
    )
    foreign = ThreadRepository(
        runs_db, ScopeContext(tenant_id="tenant-b", workspace_id="workspace-b")
    )
    await owner.create(
        Thread(
            thread_id="shared-thread",
            graph_version_ref="graph:a",
            deployment_ref="deployment:a",
            tenant_id="tenant-a",
            workspace_id="workspace-a",
        )
    )

    assert await foreign.get("shared-thread") is None
    assert await foreign.get("unknown-thread") is None
    assert await foreign.list() == []


async def test_pending_count_uses_scoped_aggregate(runs_db, monkeypatch) -> None:
    repository = RunRepository(
        runs_db, ScopeContext(tenant_id="tenant-a", workspace_id="workspace-a")
    )
    calls: list[dict[str, object]] = []
    original = BoundStructuredTable.count

    async def recording_count(self, **kwargs):
        calls.append(kwargs)
        return await original(self, **kwargs)

    monkeypatch.setattr(BoundStructuredTable, "count", recording_count)

    assert await repository.count_pending("deployment-a") == 0
    assert calls == [
        {"where": {"status": "PENDING", "deployment_ref": "deployment-a"}}
    ]
