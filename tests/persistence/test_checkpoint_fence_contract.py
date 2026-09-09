"""Every checkpoint writer obeys the installed lease, on either backend."""

import pytest

from tests.conftest import requires_docker
from zeroth.integrations.persistence.runs import RunRepository, ThreadRepository
from zeroth.platform.dispatch.lease import FencedRunWriteRejectedError, LeaseManager
from zeroth.runtime.runs import Run


@requires_docker
@pytest.mark.parametrize("write", ["put", "write_checkpoint"])
@pytest.mark.parametrize("takeover", [False, True])
@pytest.mark.parametrize("new_checkpoint", [False, True])
async def test_expired_owner_cannot_change_any_checkpoint(
    dual_database,
    write,
    takeover,
    new_checkpoint,
):
    repo = RunRepository.for_default_compatibility(dual_database)
    threads = ThreadRepository.for_default_compatibility(dual_database)
    run = await repo.create(Run(graph_version_ref="audit:v1", deployment_ref="audit"))
    manager = LeaseManager(dual_database)
    assert await manager.claim_pending("audit", "worker-a") == run.run_id
    generation = await manager.current_generation(run.run_id)
    repo.install_fence(run.run_id, "worker-a", generation)
    run = await repo.get(run.run_id)
    run.metadata["writer"] = "live"
    await getattr(repo, write)(run)
    checkpoint_id = run.checkpoint_id
    before = await repo.get_checkpoint(checkpoint_id)
    thread_before = await threads.get(run.thread_id)

    async with dual_database.transaction() as connection:
        await connection.execute(
            "UPDATE runs SET lease_expires_at = ? WHERE run_id = ?",
            ("2000-01-01T00:00:00+00:00", run.run_id),
        )
    if takeover:
        assert await manager.claim_pending("audit", "worker-b") == run.run_id
    if new_checkpoint:
        run.checkpoint_id = None
    run.metadata["writer"] = "stale"
    with pytest.raises(FencedRunWriteRejectedError):
        await getattr(repo, write)(run)

    assert await repo.get_checkpoint(checkpoint_id) == before
    assert await threads.get(run.thread_id) == thread_before
    if new_checkpoint:
        assert await repo.get_checkpoint(run.checkpoint_id) is None


async def test_standalone_checkpoint_preserves_run_and_thread_registration(sqlite_db):
    repo = RunRepository.for_default_compatibility(sqlite_db)
    threads = ThreadRepository.for_default_compatibility(sqlite_db)
    first = await repo.create(Run(graph_version_ref="audit:v1", deployment_ref="audit"))
    second = await repo.create(
        Run(graph_version_ref="audit:v1", deployment_ref="audit", thread_id=first.thread_id)
    )
    persisted = await repo.get(first.run_id)
    first.metadata["snapshot_only"] = True
    first.checkpoint_id = None
    checkpoint_id = await repo.write_checkpoint(first)

    assert (await repo.get_checkpoint(checkpoint_id)).metadata["snapshot_only"] is True
    assert await repo.get(first.run_id) == persisted
    thread = await threads.get(first.thread_id)
    assert thread.last_run_id == second.run_id
    assert thread.run_ids == [first.run_id, second.run_id]
