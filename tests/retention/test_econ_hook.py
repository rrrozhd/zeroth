"""WS-E: the econ-event erasure hook is invoked with best-effort join keys."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from tests.retention.conftest import make_audit_record
from zeroth.governance.retention import RetentionErasureService, SqlAlchemyEconEventEraser
from zeroth.governance.retention.econ_eraser import EconEventEraser
from zeroth.runtime.runs import Run
from zeroth.service.bootstrap.factory import _build_retention_econ_eraser


class _RecordingEconEraser:
    def __init__(self, deleted: int = 4) -> None:
        self.deleted = deleted
        self.called_with: list[tuple[str, list[str], str]] = []

    async def delete_events_for_run(self, tenant_id, join_keys, *, idempotency_key):
        self.called_with.append((tenant_id, list(join_keys), idempotency_key))
        return self.deleted


async def test_econ_eraser_satisfies_protocol() -> None:
    # The shipped concrete implementation IS a structural EconEventEraser.
    assert isinstance(SqlAlchemyEconEventEraser(), EconEventEraser)
    assert isinstance(_RecordingEconEraser(), EconEventEraser)


async def test_erase_run_calls_econ_hook_with_run_and_metadata_join_keys(env) -> None:
    eraser = _RecordingEconEraser(deleted=4)
    service = RetentionErasureService(
        audit_repository=env.audit_repo,
        run_repository=env.run_repo,
        policy_repository=env.policy_repo,
        legal_hold_repository=env.hold_repo,
        log_repository=env.log_repo,
        artifact_store=env.artifact_store,
        econ_eraser=eraser,
    )

    # One audit carries a business join_key in execution_metadata.
    await env.run_repo.put(
        Run(run_id="run-econ", graph_version_ref="graph:v1", deployment_ref="deploy")
    )
    rec = make_audit_record(audit_id="run-econ-a0", run_id="run-econ", node_id="n0")
    rec = rec.model_copy(update={"execution_metadata": {"join_key": "case-42"}})
    await env.audit_repo.write(rec)

    result = await service.erase_run("run-econ", "rte")

    assert eraser.called_with, "econ hook must be invoked"
    tenant_id, keys, idempotency_key = eraser.called_with[0]
    passed = set(keys)
    assert tenant_id == "default"
    assert idempotency_key
    assert "run-econ" in passed  # run_id is always a candidate key
    assert "case-42" in passed  # derived from execution_metadata
    assert result.econ_events_deleted == 4

    actions = [e["action"] for e in await env.log_repo.list_for_run("run-econ")]
    assert "econ_erase" in actions


async def test_enabled_but_unavailable_econ_cleanup_is_failed_not_skipped(
    env,
    monkeypatch,
) -> None:
    monkeypatch.setitem(sys.modules, "zeroth.econ.plane.database", None)
    eraser = _build_retention_econ_eraser(
        SimpleNamespace(regulus=SimpleNamespace(enabled=True))
    )
    service = RetentionErasureService(
        audit_repository=env.audit_repo,
        run_repository=env.run_repo,
        policy_repository=env.policy_repo,
        legal_hold_repository=env.hold_repo,
        log_repository=env.log_repo,
        artifact_store=env.artifact_store,
        econ_eraser=eraser,
    )
    await env.run_repo.put(
        Run(run_id="run-econ-unavailable", graph_version_ref="graph:v1", deployment_ref="deploy")
    )

    result = await service.erase_run("run-econ-unavailable", "rte")

    assert result.external_cleanup_status == "failed"
    assert result.econ_events_deleted == 0
    actions = [
        event["action"] for event in await env.log_repo.list_for_run("run-econ-unavailable")
    ]
    assert "econ_erase_failed" in actions
    assert "econ_erase_skipped" not in actions


async def test_sqlalchemy_econ_eraser_noop_on_empty_keys() -> None:
    # No join keys -> nothing to delete, no econ_plane import/DB required.
    eraser = SqlAlchemyEconEventEraser()
    assert await eraser.delete_events_for_run("tenant-a", [], idempotency_key="empty") == 0
    assert (
        await eraser.delete_events_for_run(
            "tenant-a",
            ["", None],
            idempotency_key="empty-2",  # type: ignore[list-item]
        )
        == 0
    )


async def test_sqlalchemy_econ_eraser_predicates_tenant_and_join_key(monkeypatch) -> None:
    from zeroth.econ.plane import database as econ_database

    statements = []

    class _Result:
        rowcount = 0

    class _Session:
        def get(self, model, operation_id):
            return None

        def execute(self, statement):
            statements.append(statement)
            return _Result()

        def commit(self):
            return None

        def add(self, receipt):
            return None

        # The eraser claims the idempotency key with an INSERT + flush before it
        # deletes anything, so the race has a row to collide on (A01-48).
        def flush(self):
            return None

        def rollback(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(econ_database, "SessionLocal", _Session)

    deleted = SqlAlchemyEconEventEraser()._delete_sync("tenant-a", ["shared-key"], "econ-operation")

    assert deleted == 0
    assert len(statements) == 2
    for statement in statements:
        compiled = statement.compile()
        assert "tenant_id" in str(compiled)
        assert "join_key" in str(compiled)
        assert "tenant-a" in compiled.params.values()
        assert ["shared-key"] in compiled.params.values()


async def test_sqlalchemy_econ_eraser_replays_durable_receipt(monkeypatch) -> None:
    from zeroth.econ.plane import database as econ_database

    receipts = {}
    execute_calls = 0

    class _Result:
        rowcount = 1

    class _Session:
        def get(self, model, operation_id):
            return receipts.get(operation_id)

        def execute(self, statement):
            nonlocal execute_calls
            execute_calls += 1
            return _Result()

        def add(self, receipt):
            receipts[receipt.operation_id] = receipt

        def commit(self):
            return None

        def flush(self):
            return None

        def rollback(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(econ_database, "SessionLocal", _Session)
    eraser = SqlAlchemyEconEventEraser()

    first = eraser._delete_sync("tenant-a", ["join"], "stable-operation")
    second = eraser._delete_sync("tenant-a", ["join"], "stable-operation")

    assert first == second == 2
    assert execute_calls == 2


async def test_sqlalchemy_econ_eraser_uses_injected_session_factory(monkeypatch) -> None:
    from zeroth.econ.plane import database as econ_database

    created = []

    class _Result:
        rowcount = 0

    class _Session:
        def get(self, model, operation_id):
            return None

        def execute(self, statement):
            return _Result()

        def add(self, receipt):
            return None

        def flush(self):
            return None

        def commit(self):
            return None

        def rollback(self):
            return None

        def close(self):
            return None

    def configured_session_factory():
        created.append("configured")
        return _Session()

    def wrong_global_session_factory():
        raise AssertionError("global econ session factory must not be used")

    monkeypatch.setattr(econ_database, "SessionLocal", wrong_global_session_factory)
    eraser = SqlAlchemyEconEventEraser(session_factory=configured_session_factory)

    assert eraser._delete_sync("tenant-a", ["join-a"], "operation-a") == 0
    assert created == ["configured"]
