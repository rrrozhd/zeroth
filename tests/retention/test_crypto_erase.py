"""WS-E: commitment-digest crypto-erasure preserves the audit hash-chain."""

from __future__ import annotations

from datetime import UTC, datetime

from tests.retention.conftest import make_audit_record

from zeroth.core.audit import AuditContinuityVerifier, AuditRepository, NodeAuditRecord
from zeroth.core.audit.verifier import _compute_record_digest, compute_chained_record
from zeroth.core.signing import EnvHmacSigner


async def test_crypto_erase_preserves_chain_verification(sqlite_db) -> None:
    """THE invariant: erase the middle of a SIGNED chain, chain still verifies.

    Post-erasure the v2 digest is unchanged (it is over the commitments, not the
    plaintext), so the WS-D signature over that digest still verifies too — the
    stronger, signed proof.
    """
    signer = EnvHmacSigner(key_id="k1", keys={"k1": b"secret"})
    repo = AuditRepository(sqlite_db, signer=signer)
    for i in range(3):
        await repo.write(make_audit_record(audit_id=f"a{i}", run_id="run-x", node_id=f"n{i}"))

    before = await AuditContinuityVerifier(repo, signer=signer).verify_run("run-x")
    assert before.verified is True
    assert before.signature_verified is True
    assert before.record_count == 3

    erased = await repo.crypto_erase("a1", reason="rte")
    assert erased is not None and erased.erased is True

    after = await AuditContinuityVerifier(repo, signer=signer).verify_run("run-x")
    assert after.verified is True  # digest continuity intact over the tombstone
    assert after.signature_verified is True  # signature over the unchanged digest
    assert after.record_count == 3  # tombstone kept, not removed


async def test_crypto_erase_removes_plaintext_keeps_commitment(sqlite_db) -> None:
    repo = AuditRepository(sqlite_db)
    await repo.write(make_audit_record(audit_id="a0", run_id="run-y"))

    original = await repo.get("a0")
    assert original.digest_version == 2
    assert original.pii_commitments  # stamped at write
    digest_before = original.record_digest

    erased = await repo.crypto_erase("a0", reason="manual")
    assert erased.input_snapshot == {}
    assert erased.output_snapshot == {}
    assert erased.validation_results == {}
    assert erased.execution_metadata == {}
    assert erased.stdout is None
    assert erased.stderr is None
    assert erased.error is None
    assert erased.tool_calls == []
    assert erased.memory_interactions == []
    # Commitments retained; digest byte-identical; tombstone flagged.
    assert erased.pii_commitments == original.pii_commitments
    assert erased.record_digest == digest_before
    assert erased.erased is True
    assert erased.erasure_reason == "manual"
    # Recompute over the erased record still matches the stored digest.
    assert _compute_record_digest(erased) == digest_before


async def test_legacy_v1_record_cannot_be_erased(sqlite_db) -> None:
    """A grandfathered digest_version=1 record is un-erasable (raises)."""
    # Forge a legacy v1 row exactly as pre-WS-E code would: whole-payload digest,
    # no commitments, digest_version=1, written straight to the table.
    legacy = NodeAuditRecord(
        audit_id="legacy-1",
        run_id="run-legacy",
        node_id="n1",
        graph_version_ref="graph:v1",
        deployment_ref="deploy",
        status="completed",
        input_snapshot={"ssn": "123-45-6789"},
        started_at=datetime(2026, 7, 11, tzinfo=UTC),
        completed_at=datetime(2026, 7, 11, 0, 0, 1, tzinfo=UTC),
    )
    assert legacy.digest_version == 1
    chained = compute_chained_record(legacy, None, None)
    from zeroth.core.storage.json import to_json_value

    async with sqlite_db.transaction() as connection:
        await connection.execute(
            """
            INSERT INTO node_audits
                (audit_id, run_id, thread_id, node_id, graph_version_ref,
                 deployment_ref, tenant_id, workspace_id, created_at, record_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chained.audit_id,
                chained.run_id,
                None,
                chained.node_id,
                chained.graph_version_ref,
                chained.deployment_ref,
                chained.tenant_id,
                None,
                datetime(2026, 7, 11, tzinfo=UTC).isoformat(),
                to_json_value(chained.model_dump(mode="json")),
            ),
        )

    repo = AuditRepository(sqlite_db)
    stored = await repo.get("legacy-1")
    assert stored.digest_version == 1
    try:
        await repo.crypto_erase("legacy-1", reason="rte")
    except ValueError as exc:
        assert "legacy" in str(exc).lower() or "digest_version=1" in str(exc)
    else:  # pragma: no cover - the raise is the contract
        raise AssertionError("legacy v1 record must not be erasable")

    # Plaintext untouched: the legacy row still holds its PII.
    still = await repo.get("legacy-1")
    assert still.input_snapshot == {"ssn": "123-45-6789"}
    assert still.erased is False


async def test_list_erasable_excludes_legacy_and_held(sqlite_db) -> None:
    repo = AuditRepository(sqlite_db)
    await repo.write(make_audit_record(audit_id="v2-a", run_id="run-a"))
    await repo.write(make_audit_record(audit_id="v2-b", run_id="run-b"))

    future = datetime(2030, 1, 1, tzinfo=UTC)
    erasable = await repo.list_erasable("default", future)
    assert {r.run_id for r in erasable} == {"run-a", "run-b"}

    excluded = await repo.list_erasable("default", future, exclude_run_ids=["run-a"])
    assert {r.run_id for r in excluded} == {"run-b"}

    # A cutoff before the records excludes them all.
    past = datetime(2000, 1, 1, tzinfo=UTC)
    assert await repo.list_erasable("default", past) == []
