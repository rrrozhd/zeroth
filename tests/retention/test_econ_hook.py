"""WS-E: the econ-event erasure hook is invoked with best-effort join keys."""

from __future__ import annotations

from tests.retention.conftest import make_audit_record

from zeroth.core.runs import Run
from zeroth.core.retention import RetentionErasureService, SqlAlchemyEconEventEraser
from zeroth.core.retention.econ_eraser import EconEventEraser


class _RecordingEconEraser:
    def __init__(self, deleted: int = 4) -> None:
        self.deleted = deleted
        self.called_with: list[list[str]] = []

    async def delete_events_for_run(self, join_keys):
        self.called_with.append(list(join_keys))
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
    passed = set(eraser.called_with[0])
    assert "run-econ" in passed  # run_id is always a candidate key
    assert "case-42" in passed  # derived from execution_metadata
    assert result.econ_events_deleted == 4

    actions = [e["action"] for e in await env.log_repo.list_for_run("run-econ")]
    assert "econ_erase" in actions


async def test_sqlalchemy_econ_eraser_noop_on_empty_keys() -> None:
    # No join keys -> nothing to delete, no econ_plane import/DB required.
    assert await SqlAlchemyEconEventEraser().delete_events_for_run([]) == 0
    assert await SqlAlchemyEconEventEraser().delete_events_for_run(["", None]) == 0  # type: ignore[list-item]
