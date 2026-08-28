"""Tamper-evident + tamper-resistant verification for audit chains.

Two independent axes:

* **digest continuity** (unkeyed SHA-256) — the historical guarantee: each
  record hashes its own content and pins its predecessor's digest, so accidental
  corruption or a naive edit breaks the chain.
* **keyed signature** (WS-D) — a signature over each record's digest, so a
  malicious writer who recomputes the digest still cannot forge acceptance
  without the signing key.

The signature is computed over the digest, and the digest MUST be byte-identical
whether or not a record is signed — otherwise signing would retroactively break
every pre-signing record's stored digest. That invariant lives in
``_compute_record_digest`` via ``_DIGEST_EXCLUDED_FIELDS`` (the WS-E seam).
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from zeroth.governance.audit.coordination import AuditChainOrderingError, order_audit_records
from zeroth.governance.audit.erasure_schema import ERASED_PII_VALUES, pii_commitment_fields
from zeroth.governance.audit.models import AuditContinuityReport, NodeAuditRecord
from zeroth.platform.signing import sign_digest, verify_digest
from zeroth.platform.storage.json import to_json_value

if TYPE_CHECKING:
    from zeroth.platform.signing import SigningKeyProvider


# --- WS-E extension seam -----------------------------------------------------
# Fields POPPED from the record before the digest is computed, so the digest is
# independent of whether the record is signed (adding a signature must not change
# the bytes a pre-signing record hashed). ``record_digest`` itself is handled
# separately: it is nulled IN PLACE (the key is kept) to reproduce the exact
# byte layout the pre-WS-D code hashed — popping it would change the JSON and
# invalidate every historical digest.
#
# WS-E (retention/erasure) extends this the same additive way: the erasure-
# metadata field names are added here, and — behind a ``digest_version >= 2``
# gate — per-field commitment hashes are substituted for the popped PII payload
# fields. Keep the rule "exclusions are additive; the v1 layout never changes"
# so old digests stay verifiable.
#
# Why every WS-E field is excluded: a pre-WS-E row's stored ``record_json`` has
# NONE of these keys, but loading it through ``NodeAuditRecord.model_validate``
# fills the defaults, so ``model_dump`` would otherwise inject them and break the
# byte layout the legacy digest was computed over. Popping them restores the
# exact pre-WS-E payload. For commitment digests the PII fields are ALSO popped and
# replaced by ``pii_commitments`` (see ``_compute_record_digest``), which makes
# the digest identical whether the plaintext is present or crypto-erased.
_DIGEST_EXCLUDED_FIELDS = frozenset(
    {
        "record_signature",
        "signing_key_id",
        "signing_algorithm",
        # WS-E erasure metadata.
        "erased",
        "erased_at",
        "erasure_reason",
        "digest_version",
        "pii_commitments",
        # Database ordering metadata, absent from historical digest payloads.
        "chain_sequence",
    }
)


# PII payload fields substituted by per-field commitment hashes when
# ``digest_version >= 2``. Nulling any of these during crypto-erasure leaves the
# digest unchanged because the digest folds in ``pii_commitments`` (stamped once
# at write time) rather than these raw values.
class AuditContinuityVerifier:
    """Verify digest continuity and (WS-D) signatures for audit history.

    ``signer`` is optional: without it the digest axis still verifies and the
    signature axis reports ``None`` (unsigned-legacy). The API threads the
    process signer in so signed records are checked against the real key.
    """

    def __init__(self, repository, signer: SigningKeyProvider | None = None) -> None:  # noqa: ANN001
        self._repository = repository
        self._signer = signer

    async def verify_run(
        self,
        run_id: str,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None = None,
        workspace_scoped: bool = False,
        deployment_ref: str | None = None,
    ) -> AuditContinuityReport:
        """Verify digest continuity and signatures for one run's audit chain.

        Records are strictly ordered first; an ordering violation yields a
        failed report (``verified=False``) rather than an exception.
        """
        scope = {}
        if tenant_id is not None or workspace_scoped or deployment_ref is not None:
            scope = {
                "tenant_id": tenant_id,
                "workspace_id": workspace_id,
                "workspace_scoped": workspace_scoped,
                "deployment_ref": deployment_ref,
            }
        records = await self._repository.list_by_run(run_id, **scope)
        try:
            ordered = order_audit_records(records, strict=True)
        except AuditChainOrderingError as exc:
            return AuditContinuityReport(
                scope=f"run:{run_id}",
                verified=False,
                record_count=len(records),
                error=str(exc),
            )
        return self._verify_records(scope=f"run:{run_id}", records=ordered)

    async def verify_deployment(
        self,
        deployment_ref: str,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None = None,
        workspace_scoped: bool = False,
    ) -> AuditContinuityReport:
        """Verify every run recorded under ``deployment_ref``.

        Chains are per-run: each run is ordered and verified independently,
        with per-run signature states aggregated into one three-state result.
        Verification loads the complete tenant/workspace-scoped run chain because
        legacy service-request identities were shared across deployments.  The
        response count remains the number of records attributed to this
        deployment; no records from another deployment are returned.  The first
        failing run short-circuits into a failed deployment report.
        """
        scope = {}
        if tenant_id is not None or workspace_scoped:
            scope = {
                "tenant_id": tenant_id,
                "workspace_id": workspace_id,
                "workspace_scoped": workspace_scoped,
            }
        records = await self._repository.list_by_deployment(deployment_ref, **scope)
        if not records:
            return AuditContinuityReport(
                scope=f"deployment:{deployment_ref}",
                verified=True,
                record_count=0,
            )

        by_run: dict[str, list[NodeAuditRecord]] = {}
        for record in records:
            by_run.setdefault(record.run_id, []).append(record)

        total = 0
        total_unsigned = 0
        signature_states: list[bool | None] = []
        for run_id in sorted(by_run):
            run_records = await self._repository.list_by_run(run_id, **scope)
            try:
                ordered = order_audit_records(run_records, strict=True)
            except AuditChainOrderingError as exc:
                return AuditContinuityReport(
                    scope=f"deployment:{deployment_ref}",
                    verified=False,
                    record_count=total + len(by_run[run_id]),
                    error=str(exc),
                    unsigned_record_count=total_unsigned,
                )
            report = self._verify_records(
                scope=f"run:{run_id}",
                records=ordered,
            )
            deployment_records = by_run[run_id]
            total += len(deployment_records)
            total_unsigned += sum(
                record.signing_key_id is None for record in deployment_records
            )
            signature_states.append(report.signature_verified)
            if not report.verified:
                return AuditContinuityReport(
                    scope=f"deployment:{deployment_ref}",
                    verified=False,
                    record_count=total,
                    failed_audit_id=report.failed_audit_id,
                    error=report.error,
                    signature_verified=report.signature_verified,
                    unsigned_record_count=total_unsigned,
                )
        return AuditContinuityReport(
            scope=f"deployment:{deployment_ref}",
            verified=True,
            record_count=total,
            signature_verified=_aggregate_signature_states(signature_states),
            unsigned_record_count=total_unsigned,
        )

    def _verify_records(
        self,
        *,
        scope: str,
        records: list[NodeAuditRecord],
    ) -> AuditContinuityReport:
        """Walk an ordered chain checking predecessor links, digests, and signatures.

        The digest and signature axes are independent: an unsigned-legacy record
        (no ``signing_key_id``) is counted separately and never fails the
        signature axis.
        """
        previous_digest: str | None = None
        signed_count = 0
        signature_failed = False
        for record in records:
            if record.previous_record_digest != previous_digest:
                return AuditContinuityReport(
                    scope=scope,
                    verified=False,
                    record_count=len(records),
                    failed_audit_id=record.audit_id,
                    error="previous digest mismatch",
                )
            expected_digest = _compute_record_digest(record)
            digest_matches = record.record_digest == expected_digest
            # ``campaign_id`` was added while v3 records were already in the
            # field.  Pydantic supplies ``None`` when loading those historical
            # JSON objects, but the original signed byte layout had no key at
            # all.  Accept that one semantically equivalent v3 layout only when
            # the campaign is null; non-null campaign correlation remains bound
            # by the current digest and cannot fall back to the legacy layout.
            if (
                not digest_matches
                and record.digest_version == 3
                and record.campaign_id is None
            ):
                digest_matches = record.record_digest == _compute_record_digest(
                    record,
                    _omit_campaign_id=True,
                )
            if not digest_matches:
                return AuditContinuityReport(
                    scope=scope,
                    verified=False,
                    record_count=len(records),
                    failed_audit_id=record.audit_id,
                    error="record digest mismatch",
                )
            # Erasure-forgery gate (WS-E integrity). `erased` is a digest-excluded
            # field and an erased v2+ record's digest folds in the STORED
            # commitments rather than live PII (see _compute_record_digest), so a
            # DB-only attacker can flip erased=True and rewrite the PII payload
            # without breaking the digest or its signature. Legitimate erasure
            # always nulls PII to the erased sentinels (repository redact path);
            # any populated PII field on a record claiming to be erased is a
            # forgery, so refuse it here rather than trust the stored commitments.
            if erased_field := _erased_record_carrying_pii(record):
                return AuditContinuityReport(
                    scope=scope,
                    verified=False,
                    record_count=len(records),
                    failed_audit_id=record.audit_id,
                    error=f"erased record carries PII in {erased_field}",
                )
            # Signature axis: independent of digest continuity. A record counts as
            # "signed" iff it carries a key id; an unsigned-legacy record neither
            # passes nor fails — it is reported separately.
            if record.signing_key_id is not None:
                signed_count += 1
                if not verify_digest(
                    record.record_digest or "",
                    record.record_signature,
                    record.signing_key_id,
                    record.signing_algorithm,
                    self._signer,
                ):
                    signature_failed = True
            previous_digest = record.record_digest
        return AuditContinuityReport(
            scope=scope,
            verified=True,
            record_count=len(records),
            signature_verified=_resolve_signature_state(signed_count, signature_failed),
            unsigned_record_count=len(records) - signed_count,
        )


def compute_chained_record(
    record: NodeAuditRecord,
    previous_digest: str | None,
    signer: SigningKeyProvider | None = None,
) -> NodeAuditRecord:
    """Fill chain + (optional) signature fields deterministically.

    Computes ``record_digest`` over the predecessor-linked record, then signs
    ``signable_bytes(record_digest, key_id, alg)``. When ``signer`` is None (or a
    :class:`NullSigner`) the signature fields stay None — unsigned-legacy, never
    signed-invalid.
    """
    chained = record.model_copy(
        update={
            "previous_record_digest": previous_digest,
            "record_digest": None,
            "record_signature": None,
            "signing_key_id": None,
            "signing_algorithm": None,
        }
    )
    digest = _compute_record_digest(chained)
    signature, key_id, algorithm = sign_digest(digest, signer)
    return chained.model_copy(
        update={
            "record_digest": digest,
            "record_signature": signature,
            "signing_key_id": key_id,
            "signing_algorithm": algorithm,
        }
    )


def _canonical(value: object) -> str:
    """Canonical JSON (sorted keys, no whitespace) — matches ``to_json_value``.

    Used for the commitment inputs so the commitment covers exactly the canonical
    representation the digest hashes over.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _compute_pii_commitments(record: NodeAuditRecord) -> dict[str, str]:
    """Per-field commitment hashes: ``sha256(canonical_json(plaintext))``.

    THE single canonical hashing shared by the write path (repository, at
    write time) and the verify path (``_compute_record_digest``, on every
    non-erased v2 record). Both must fold in identical commitments or the
    digest — and the signature over it — would diverge between write and verify.

    Computed from a single ``model_dump(mode="json")`` so list-of-model fields
    (tool_calls, memory_interactions) and datetime-bearing dicts serialize
    exactly as the digest sees them — never by passing raw models to a helper
    that only handles a top-level BaseModel.
    """
    dumped = record.model_dump(mode="json")
    return {
        field: hashlib.sha256(_canonical(dumped[field]).encode("utf-8")).hexdigest()
        for field in pii_commitment_fields(record.digest_version or 1)
    }


def _erased_record_carrying_pii(record: NodeAuditRecord) -> str | None:
    """First PII field a record claims-erased yet still populates, else ``None``.

    Only meaningful for ``digest_version >= 2`` (commitment digests): those are
    the records whose digest is computed from stored commitments when erased, so
    a flipped ``erased`` flag plus rewritten PII survives digest+signature
    verification. A legitimately erased record has every commitment field reset
    to its :data:`ERASED_PII_VALUES` sentinel; the first field that differs is
    returned as evidence of forgery.
    """
    if not record.erased or (record.digest_version or 1) < 2:
        return None
    dumped = record.model_dump(mode="json")
    for field in pii_commitment_fields(record.digest_version or 1):
        if dumped.get(field) != ERASED_PII_VALUES[field]:
            return field
    return None


def _compute_record_digest(
    record: NodeAuditRecord,
    *,
    _omit_campaign_id: bool = False,
) -> str:
    """Recompute a record's digest under the historical byte layout.

    ``record_digest`` is nulled in place, WS-D/WS-E fields are popped (see
    ``_DIGEST_EXCLUDED_FIELDS``), and for ``digest_version >= 2`` the PII fields
    are replaced by commitment hashes — recomputed from live plaintext for
    non-erased records, taken from the stored commitments for erased ones.
    """
    payload = record.model_dump(mode="json")
    # Historical layout: record_digest is nulled IN PLACE (key retained).
    payload["record_digest"] = None
    # WS-D/WS-E seam: signature + erasure-metadata fields are POPPED so the
    # digest is byte-identical with or without them. For v1 records this restores
    # the exact pre-WS-E payload (grandfathered).
    for field in _DIGEST_EXCLUDED_FIELDS:
        payload.pop(field, None)
    if _omit_campaign_id:
        payload.pop("campaign_id", None)
    # WS-E commitment digests: substitute per-field commitment hashes for the
    # raw PII payload fields.
    #
    # G3 (content-integrity): the commitment folded in must BIND the live PII,
    # not merely echo the record's stored ``pii_commitments`` — otherwise an
    # attacker could edit a PII field, leave pii_commitments/digest/signature
    # untouched, and still verify. So for a NON-erased record we RECOMPUTE the
    # commitments from the record's current PII fields (a tampered field yields a
    # different commitment -> a different digest -> mismatch, and the signature
    # over the original digest also breaks). For an ERASED record the plaintext
    # is gone, so there is nothing live to recompute — we trust the STORED
    # commitments stamped at write time, which keeps the append-only chain (and
    # its signature) verifiable after right-to-erasure. At write time the record
    # is non-erased and its live PII equals what produced the stored commitments,
    # so recomputed == stored — write and verify agree.
    if (record.digest_version or 1) >= 2:
        for field in pii_commitment_fields(record.digest_version or 1):
            payload.pop(field, None)
        if record.erased:
            payload["pii_commitments"] = record.pii_commitments or {}
        else:
            payload["pii_commitments"] = _compute_pii_commitments(record)
    return hashlib.sha256(to_json_value(payload).encode("utf-8")).hexdigest()


def _resolve_signature_state(signed_count: int, signature_failed: bool) -> bool | None:
    """Map per-scope signature counts to the three-state result."""
    if signed_count == 0:
        return None  # unsigned-legacy: neither verified nor tampered
    # Signed records present: True only when none failed verification.
    return not signature_failed


def _aggregate_signature_states(states: list[bool | None]) -> bool | None:
    """Combine per-run signature states for a deployment-wide report."""
    if any(state is False for state in states):
        return False
    if all(state is None for state in states):
        return None
    return True
