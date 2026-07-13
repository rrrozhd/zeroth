from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from tests.conftest import requires_docker
from zeroth.core.audit import AuditContinuityVerifier, AuditRepository, NodeAuditRecord
from zeroth.core.audit import coordination as audit_coordination
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


async def _insert_null_sequence_tail(
    database, record: NodeAuditRecord, created_at: datetime
) -> None:
    payload = record.model_dump(mode="json")
    payload.pop("chain_sequence")
    async with database.transaction() as connection:
        await connection.execute(
            """
            INSERT INTO node_audits (
                audit_id, run_id, thread_id, node_id, graph_version_ref,
                deployment_ref, tenant_id, workspace_id, created_at,
                chain_sequence, record_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                record.audit_id,
                record.run_id,
                record.thread_id,
                record.node_id,
                record.graph_version_ref,
                record.deployment_ref,
                record.tenant_id,
                record.workspace_id,
                created_at.isoformat(),
                json.dumps(payload, sort_keys=True),
            ),
        )


async def test_mixed_rolling_writer_chain_recovers_linked_tail(sqlite_db) -> None:
    repository = AuditRepository(sqlite_db)
    run_id = "run:mixed-rolling-writers"
    timestamp = datetime(2026, 7, 12, tzinfo=UTC)

    sequenced_one = await repository.write(
        _record(audit_id="mixed:seq:1", run_id=run_id, started_at=timestamp)
    )
    null_tail_one = compute_chained_record(
        _record(
            audit_id="mixed:null:1",
            run_id=run_id,
            started_at=timestamp - timedelta(days=1),
        ),
        sequenced_one.record_digest,
    )
    await _insert_null_sequence_tail(sqlite_db, null_tail_one, timestamp + timedelta(seconds=1))

    sequenced_two = await repository.write(
        _record(
            audit_id="mixed:seq:2",
            run_id=run_id,
            started_at=timestamp - timedelta(days=2),
        )
    )
    assert sequenced_two.chain_sequence == 2
    assert sequenced_two.previous_record_digest == null_tail_one.record_digest

    null_tail_two = compute_chained_record(
        _record(
            audit_id="mixed:null:2",
            run_id=run_id,
            started_at=timestamp - timedelta(days=3),
        ),
        sequenced_two.record_digest,
    )
    await _insert_null_sequence_tail(sqlite_db, null_tail_two, timestamp + timedelta(seconds=3))

    sequenced_three = await repository.write(
        _record(
            audit_id="mixed:seq:3",
            run_id=run_id,
            started_at=timestamp - timedelta(days=4),
        )
    )
    assert sequenced_three.chain_sequence == 3
    assert sequenced_three.previous_record_digest == null_tail_two.record_digest

    records = await repository.list_by_run(run_id)
    assert [record.audit_id for record in records] == [
        "mixed:seq:1",
        "mixed:null:1",
        "mixed:seq:2",
        "mixed:null:2",
        "mixed:seq:3",
    ]
    assert [record.chain_sequence for record in records] == [1, None, 2, None, 3]
    report = await AuditContinuityVerifier(repository).verify_run(run_id)
    assert report.verified is True
    assert report.record_count == 5

    async with sqlite_db.transaction() as connection:
        head = await connection.fetch_one(
            "SELECT head_digest, next_sequence FROM audit_chain_heads WHERE run_id = ?",
            (run_id,),
        )
    assert head == {"head_digest": sequenced_three.record_digest, "next_sequence": 4}


async def test_sequenced_run_query_uses_index_without_temp_sort(sqlite_db) -> None:
    assert hasattr(audit_coordination, "SEQUENCED_RUN_ROWS_SQL")
    sql = audit_coordination.SEQUENCED_RUN_ROWS_SQL
    async with sqlite_db.transaction() as connection:
        plan = await connection.fetch_all(f"EXPLAIN QUERY PLAN {sql}", ("run:indexed",))

    details = " ".join(str(row["detail"]) for row in plan).upper()
    assert "UQ_NODE_AUDITS_RUN_CHAIN_SEQUENCE" in details
    assert "TEMP B-TREE" not in details


def test_ambiguous_mixed_chain_has_deterministic_fallback_and_strict_error() -> None:
    timestamp = datetime(2026, 7, 12, tzinfo=UTC)
    root = compute_chained_record(
        _record(audit_id="fork:root", run_id="run:fork", started_at=timestamp).model_copy(
            update={"chain_sequence": 1}
        ),
        None,
    )
    sequenced_branch = compute_chained_record(
        _record(
            audit_id="fork:sequenced",
            run_id="run:fork",
            started_at=timestamp,
        ).model_copy(update={"chain_sequence": 2}),
        root.record_digest,
    )
    legacy_branch = compute_chained_record(
        _record(audit_id="fork:legacy", run_id="run:fork", started_at=timestamp),
        root.record_digest,
    )
    input_order = [root, sequenced_branch, legacy_branch]

    assert audit_coordination.order_audit_records(input_order) == input_order
    with pytest.raises(audit_coordination.AuditChainOrderingError, match="forks"):
        audit_coordination.order_audit_records(input_order, strict=True)


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
    assert appended.chain_sequence == 1
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
async def test_postgres_recovers_null_tail_from_rolling_writer(postgres_database) -> None:
    repository = AuditRepository(postgres_database)
    run_id = "run:postgres-mixed-rolling"
    timestamp = datetime(2026, 7, 12, tzinfo=UTC)
    try:
        sequenced = await repository.write(
            _record(audit_id="postgres:mixed:seq", run_id=run_id, started_at=timestamp)
        )
        null_tail = compute_chained_record(
            _record(
                audit_id="postgres:mixed:null",
                run_id=run_id,
                started_at=timestamp - timedelta(days=1),
            ),
            sequenced.record_digest,
        )
        await _insert_null_sequence_tail(
            postgres_database,
            null_tail,
            timestamp + timedelta(seconds=1),
        )

        appended = await repository.write(
            _record(
                audit_id="postgres:mixed:seq:2",
                run_id=run_id,
                started_at=timestamp - timedelta(days=2),
            )
        )
        assert appended.chain_sequence == 2
        assert appended.previous_record_digest == null_tail.record_digest
        assert (await AuditContinuityVerifier(repository).verify_run(run_id)).verified is True
    finally:
        async with postgres_database.transaction() as connection:
            await connection.execute("DELETE FROM node_audits WHERE run_id = ?", (run_id,))
            await connection.execute("DELETE FROM audit_chain_heads WHERE run_id = ?", (run_id,))


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
