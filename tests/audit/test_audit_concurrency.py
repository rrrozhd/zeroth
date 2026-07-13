from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from tests.conftest import requires_docker
from zeroth.core.audit import AuditContinuityVerifier, AuditRepository, NodeAuditRecord
from zeroth.core.audit.verifier import compute_chained_record
from zeroth.core.storage.async_postgres import AsyncPostgresDatabase


def _record(*, audit_id: str, run_id: str, started_at: datetime) -> NodeAuditRecord:
    return NodeAuditRecord(
        audit_id=audit_id,
        run_id=run_id,
        thread_id="thread-audit-concurrency",
        node_id=audit_id,
        graph_version_ref="graph:v1",
        deployment_ref="deployment:v1",
        status="completed",
        started_at=started_at,
        completed_at=started_at,
    )


def test_chain_sequence_is_digest_excluded_for_payload_compatibility() -> None:
    timestamp = datetime(2026, 7, 12, tzinfo=UTC)
    record = _record(audit_id="audit:digest-compatible", run_id="run:digest", started_at=timestamp)

    without_sequence = compute_chained_record(record, None)
    with_sequence = compute_chained_record(record.model_copy(update={"chain_sequence": 42}), None)

    assert with_sequence.record_digest == without_sequence.record_digest


async def test_two_repositories_allocate_one_linear_sequence(sqlite_db) -> None:
    repository_a = AuditRepository(sqlite_db)
    repository_b = AuditRepository(sqlite_db)
    run_id = "run:coordinated"
    timestamp = datetime(2026, 7, 12, tzinfo=UTC)
    records = [
        _record(
            audit_id=f"audit:{index:02d}",
            run_id=run_id,
            # Application timestamps are deliberately equal/reversed. They must
            # not influence persistence order or chain continuity.
            started_at=timestamp - timedelta(seconds=index // 2),
        )
        for index in range(20)
    ]

    await asyncio.gather(
        *(
            (repository_a if index % 2 == 0 else repository_b).write(record)
            for index, record in enumerate(records)
        )
    )

    persisted = await repository_a.list_by_run(run_id)
    assert len(persisted) == 20
    assert [record.chain_sequence for record in persisted] == list(range(1, 21))

    async with sqlite_db.transaction() as connection:
        rows = await connection.fetch_all(
            "SELECT chain_sequence FROM node_audits WHERE run_id = ? ORDER BY chain_sequence",
            (run_id,),
        )
    assert [row["chain_sequence"] for row in rows] == list(range(1, 21))

    report = await AuditContinuityVerifier(repository_b).verify_run(run_id)
    assert report.verified is True
    assert report.record_count == 20


async def test_duplicate_id_rolls_back_head_before_next_valid_append(sqlite_db) -> None:
    repository = AuditRepository(sqlite_db)
    run_id = "run:rollback"
    timestamp = datetime(2026, 7, 12, tzinfo=UTC)
    first = await repository.write(
        _record(audit_id="audit:rollback:1", run_id=run_id, started_at=timestamp)
    )

    async with sqlite_db.transaction() as connection:
        head_before = await connection.fetch_one(
            "SELECT head_digest, next_sequence FROM audit_chain_heads WHERE run_id = ?",
            (run_id,),
        )

    with pytest.raises(ValueError, match="audit_id"):
        await repository.write(
            _record(
                audit_id="audit:rollback:1",
                run_id=run_id,
                started_at=timestamp + timedelta(seconds=1),
            )
        )

    async with sqlite_db.transaction() as connection:
        head_after_failure = await connection.fetch_one(
            "SELECT head_digest, next_sequence FROM audit_chain_heads WHERE run_id = ?",
            (run_id,),
        )
    assert head_after_failure == head_before

    second = await repository.write(
        _record(
            audit_id="audit:rollback:2",
            run_id=run_id,
            started_at=timestamp + timedelta(seconds=2),
        )
    )
    assert second.chain_sequence == 2
    assert second.previous_record_digest == first.record_digest


async def test_hydration_uses_dedicated_sequence_and_null_legacy_fallback(sqlite_db) -> None:
    repository = AuditRepository(sqlite_db)
    run_id = "run:hydration"
    timestamp = datetime(2026, 7, 12, tzinfo=UTC)
    persisted = await repository.write(
        _record(audit_id="audit:hydrated", run_id=run_id, started_at=timestamp)
    )
    payload = persisted.model_dump(mode="json")
    payload.pop("chain_sequence")
    async with sqlite_db.transaction() as connection:
        await connection.execute(
            "UPDATE node_audits SET record_json = ? WHERE audit_id = ?",
            (json.dumps(payload, sort_keys=True), persisted.audit_id),
        )
    hydrated = await repository.get(persisted.audit_id)
    assert hydrated is not None
    assert hydrated.chain_sequence == 1

    legacy_run_id = "run:legacy-null"
    legacy_first = compute_chained_record(
        _record(audit_id="legacy:1", run_id=legacy_run_id, started_at=timestamp),
        None,
    )
    legacy_second = compute_chained_record(
        _record(
            audit_id="legacy:2",
            run_id=legacy_run_id,
            started_at=timestamp + timedelta(seconds=1),
        ),
        legacy_first.record_digest,
    )
    async with sqlite_db.transaction() as connection:
        for offset, legacy in enumerate((legacy_first, legacy_second)):
            legacy_payload = legacy.model_dump(mode="json")
            legacy_payload.pop("chain_sequence")
            await connection.execute(
                """
                INSERT INTO node_audits (
                    audit_id, run_id, thread_id, node_id, graph_version_ref,
                    deployment_ref, tenant_id, workspace_id, created_at,
                    chain_sequence, record_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    legacy.audit_id,
                    legacy.run_id,
                    legacy.thread_id,
                    legacy.node_id,
                    legacy.graph_version_ref,
                    legacy.deployment_ref,
                    legacy.tenant_id,
                    legacy.workspace_id,
                    (timestamp + timedelta(seconds=offset)).isoformat(),
                    json.dumps(legacy_payload, sort_keys=True),
                ),
            )

    legacy_records = await repository.list_by_run(legacy_run_id)
    assert [record.audit_id for record in legacy_records] == ["legacy:1", "legacy:2"]
    assert [record.chain_sequence for record in legacy_records] == [None, None]
    assert (await AuditContinuityVerifier(repository).verify_run(legacy_run_id)).verified is True

    appended = await repository.write(
        _record(
            audit_id="legacy:3",
            run_id=legacy_run_id,
            started_at=timestamp - timedelta(days=1),
        )
    )
    assert appended.chain_sequence == 3
    assert appended.previous_record_digest == legacy_second.record_digest
    assert (await AuditContinuityVerifier(repository).verify_run(legacy_run_id)).verified is True


async def test_deployment_verifier_sorts_each_run_by_chain_sequence() -> None:
    timestamp = datetime(2026, 7, 12, tzinfo=UTC)
    first = compute_chained_record(
        _record(audit_id="deploy:1", run_id="run:deployment", started_at=timestamp).model_copy(
            update={"chain_sequence": 1}
        ),
        None,
    )
    second = compute_chained_record(
        _record(
            audit_id="deploy:2",
            run_id="run:deployment",
            started_at=timestamp - timedelta(days=1),
        ).model_copy(update={"chain_sequence": 2}),
        first.record_digest,
    )

    class ReversedDeploymentRepository:
        async def list_by_deployment(self, deployment_ref: str) -> list[NodeAuditRecord]:
            assert deployment_ref == "deployment:v1"
            return [second, first]

    report = await AuditContinuityVerifier(ReversedDeploymentRepository()).verify_deployment(
        "deployment:v1"
    )
    assert report.verified is True
    assert report.record_count == 2


@pytest.mark.postgres
@requires_docker
async def test_two_postgres_pools_allocate_one_linear_sequence(
    postgres_database,
    postgres_container,
) -> None:
    url = postgres_container.get_connection_url()
    dsn = url.replace("postgresql+psycopg2://", "postgresql://")
    peer_database = await AsyncPostgresDatabase.create(dsn, min_size=1, max_size=2)
    try:
        repository_a = AuditRepository(postgres_database)
        repository_b = AuditRepository(peer_database)
        run_id = "run:postgres-coordinated"
        timestamp = datetime(2026, 7, 12, tzinfo=UTC)
        await asyncio.gather(
            *(
                (repository_a if index % 2 == 0 else repository_b).write(
                    _record(
                        audit_id=f"postgres:{index:02d}",
                        run_id=run_id,
                        started_at=timestamp - timedelta(seconds=index),
                    )
                )
                for index in range(20)
            )
        )

        records = await repository_a.list_by_run(run_id)
        assert [record.chain_sequence for record in records] == list(range(1, 21))
        assert (await AuditContinuityVerifier(repository_b).verify_run(run_id)).verified is True
    finally:
        await peer_database.close()
        async with postgres_database.transaction() as connection:
            await connection.execute(
                "DELETE FROM node_audits WHERE run_id = ?",
                ("run:postgres-coordinated",),
            )
            await connection.execute(
                "DELETE FROM audit_chain_heads WHERE run_id = ?",
                ("run:postgres-coordinated",),
            )
