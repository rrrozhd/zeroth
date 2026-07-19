"""WS-D: audit-chain records are signed, digest stays signing-independent."""

from __future__ import annotations

from datetime import UTC, datetime

from zeroth.core.audit import AuditContinuityVerifier, AuditRepository, NodeAuditRecord
from zeroth.core.audit.verifier import _compute_record_digest, compute_chained_record
from zeroth.core.signing import EnvHmacSigner
from zeroth.platform.storage.json import to_json_value


def _record(*, audit_id: str, run_id: str, node_id: str = "n1") -> NodeAuditRecord:
    return NodeAuditRecord(
        audit_id=audit_id,
        run_id=run_id,
        node_id=node_id,
        graph_version_ref="graph:v1",
        deployment_ref="deploy",
        status="completed",
        started_at=datetime(2026, 7, 11, tzinfo=UTC),
        completed_at=datetime(2026, 7, 11, 0, 0, 1, tzinfo=UTC),
    )


def test_digest_is_byte_identical_with_and_without_signing() -> None:
    base = _record(audit_id="a1", run_id="run-x")
    signer = EnvHmacSigner(key_id="k1", keys={"k1": b"secret"})

    unsigned = compute_chained_record(base, None, None)
    signed = compute_chained_record(base, None, signer)

    # The signature covers the digest; the digest must NOT change when a record
    # is signed, or signing would break every pre-signing record's stored digest.
    assert unsigned.record_digest == signed.record_digest
    assert unsigned.record_signature is None and unsigned.signing_key_id is None
    assert signed.record_signature and signed.signing_key_id == "k1"
    # Recompute is stable even with the signature fields present on the model.
    assert _compute_record_digest(signed) == signed.record_digest


async def test_chain_records_signed_and_verify_true(sqlite_db) -> None:
    signer = EnvHmacSigner(key_id="k1", keys={"k1": b"secret"})
    repo = AuditRepository(sqlite_db, signer=signer)
    await repo.write(_record(audit_id="a1", run_id="run-signed"))
    await repo.write(_record(audit_id="a2", run_id="run-signed", node_id="n2"))

    stored = await repo.get("a1")
    assert stored.signing_key_id == "k1" and stored.signing_algorithm == "HS256"
    assert stored.record_signature

    report = await AuditContinuityVerifier(repo, signer=signer).verify_run("run-signed")
    assert report.verified is True
    assert report.signature_verified is True
    assert report.unsigned_record_count == 0
    assert report.record_count == 2


async def test_unsigned_chain_is_three_state_none(sqlite_db) -> None:
    """Legacy records (no signer) verify as signature_verified None, not green/red."""
    repo = AuditRepository(sqlite_db)  # no signer -> unsigned-legacy
    await repo.write(_record(audit_id="a1", run_id="run-legacy"))
    await repo.write(_record(audit_id="a2", run_id="run-legacy", node_id="n2"))

    stored = await repo.get("a1")
    assert stored.record_signature is None and stored.signing_key_id is None

    report = await AuditContinuityVerifier(repo).verify_run("run-legacy")
    assert report.verified is True  # digest chain intact
    assert report.signature_verified is None  # neither verified nor tampered
    assert report.unsigned_record_count == 2


async def test_tamper_and_recompute_digest_fails_signature_without_key(sqlite_db) -> None:
    """Adversary edits a record AND recomputes its digest (defeating the digest
    axis) but cannot re-sign without the key -> the signature axis catches it."""
    signer = EnvHmacSigner(key_id="k1", keys={"k1": b"issuer-key"})
    repo = AuditRepository(sqlite_db, signer=signer)
    await repo.write(_record(audit_id="a1", run_id="run-tamper"))

    stored = await repo.get("a1")
    # Tamper the content, recompute the UNKEYED digest so continuity still holds,
    # and keep the OLD signature (over the original digest) — a forger has no key.
    tampered = stored.model_copy(update={"status": "tampered-by-attacker"})
    tampered = tampered.model_copy(update={"record_digest": _compute_record_digest(tampered)})
    async with sqlite_db.transaction() as connection:
        await connection.execute(
            "UPDATE node_audits SET record_json = ? WHERE audit_id = ?",
            (to_json_value(tampered.model_dump(mode="json")), "a1"),
        )

    report = await AuditContinuityVerifier(repo, signer=signer).verify_run("run-tamper")
    assert report.verified is True  # digest axis fooled by the recompute
    assert report.signature_verified is False  # signature axis is not
    assert report.unsigned_record_count == 0


async def test_tamper_pii_leaving_commitments_and_signature_is_detected(sqlite_db) -> None:
    """G3: editing a PII field while leaving pii_commitments/record_digest/
    record_signature untouched MUST be caught.

    A signed commitment digest binds the LIVE PII: verify recomputes the per-field
    commitments from the record's current PII and folds THOSE into the digest.
    An attacker who edits ``input_snapshot`` but keeps the stored commitments,
    digest, and signature (the classic content-integrity attack) now yields a
    recomputed commitment that no longer matches the stale one — the digest
    mismatches (and the signature over the original digest breaks too).
    """
    signer = EnvHmacSigner(key_id="k1", keys={"k1": b"issuer-key"})
    repo = AuditRepository(sqlite_db, signer=signer)
    original = NodeAuditRecord(
        audit_id="a1",
        run_id="run-pii",
        node_id="n1",
        graph_version_ref="graph:v1",
        deployment_ref="deploy",
        status="completed",
        input_snapshot={"ssn": "123-45-6789", "name": "Jane Doe"},
        started_at=datetime(2026, 7, 11, tzinfo=UTC),
        completed_at=datetime(2026, 7, 11, 0, 0, 1, tzinfo=UTC),
    )
    await repo.write(original)

    stored = await repo.get("a1")
    assert stored.digest_version == 3
    assert stored.erased is False
    assert stored.record_signature  # signed

    # Alter a PII field ONLY — commitments, digest, and signature are left
    # byte-for-byte as written. Nothing an attacker without the key can change.
    tampered = stored.model_copy(update={"input_snapshot": {"ssn": "999-99-9999"}})
    assert tampered.pii_commitments == stored.pii_commitments
    assert tampered.record_digest == stored.record_digest
    assert tampered.record_signature == stored.record_signature
    async with sqlite_db.transaction() as connection:
        await connection.execute(
            "UPDATE node_audits SET record_json = ? WHERE audit_id = ?",
            (to_json_value(tampered.model_dump(mode="json")), "a1"),
        )

    report = await AuditContinuityVerifier(repo, signer=signer).verify_run("run-pii")
    # Digest recompute over the tampered PII no longer matches the stored digest;
    # the mismatch is caught before the signature axis is even consulted.
    assert report.verified is False
    assert report.failed_audit_id == "a1"
