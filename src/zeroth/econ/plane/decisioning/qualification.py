"""Server-retained qualification registry contracts and fail-closed resolution."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from fractions import Fraction
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from zeroth.econ.plane.decisioning.models import QualificationRegistryRecord
from zeroth.econ.plane.scoped_session import ScopedSession

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")
_EXACT_FRACTION = re.compile(r"^(0|[1-9][0-9]*)/[1-9][0-9]*$")
_SUPPORTED_FAMILIES = frozenset({"beta_binomial", "two_point_incident"})


def _canonical_datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("qualification timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _canonical_value(key: str, value: object) -> object:
    if isinstance(value, datetime):
        return _canonical_datetime(value)
    if key in {"cluster_family_set", "grouping_keys"}:
        return sorted(value) if isinstance(value, (list, tuple)) else value
    if isinstance(value, tuple):
        return list(value)
    return value


def cluster_size_vector_digest(values: Sequence[int]) -> str:
    """Hash sizes in canonical cluster-key order using compact JSON."""
    payload = json.dumps(list(values), separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def qualification_record_digest(values: Mapping[str, object]) -> str:
    """Hash every immutable qualification fact using canonical JSON."""
    payload = {
        key: _canonical_value(key, value)
        for key, value in values.items()
        if key not in {"record_digest", "revoked_at"}
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class QualificationRecordV1(BaseModel):
    """Versioned server-owned certificate applicability and evidence facts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    record_version: Literal["qualification-record-v1"] = "qualification-record-v1"
    qualification_id: str = Field(min_length=1, max_length=192)
    tenant_id: str = Field(min_length=1, max_length=128)
    workload: str = Field(min_length=1, max_length=128)
    incumbent_model: str = Field(min_length=1, max_length=255)
    candidate_model: str = Field(min_length=1, max_length=255)
    policy_digest: str
    algorithm_version: str = Field(min_length=1, max_length=128)
    cluster_family_set: tuple[str, ...] = Field(min_length=1, max_length=16)
    mean_probability_contract: str = Field(max_length=128)
    icc_upper_bound_exact: str = Field(max_length=128)
    cluster_size_vector: tuple[int, ...] = Field(min_length=1, max_length=10_000)
    cluster_size_vector_sha256: str
    authorization_count_threshold: int = Field(ge=1)
    boundary_error_alpha_exact: str = Field(max_length=128)
    certificate_artifact_sha256: str
    family_confidence_set_method: str = Field(min_length=1, max_length=128)
    family_qualification_artifact_sha256: str
    icc_upper_confidence_method: str = Field(min_length=1, max_length=128)
    icc_qualification_artifact_sha256: str
    independent_unit_definition: str = Field(min_length=1, max_length=512)
    grouping_keys: tuple[str, ...] = Field(min_length=1, max_length=32)
    source_window_start: datetime
    source_window_end: datetime
    source_evidence_sha256: str
    issuer: str = Field(min_length=1, max_length=128)
    issued_at: datetime
    valid_from: datetime
    valid_until: datetime
    revoked_at: datetime | None = None
    supersedes_qualification_id: str | None = Field(default=None, max_length=192)
    record_digest: str

    @field_validator("qualification_id", "supersedes_qualification_id")
    @classmethod
    def _identifier_is_safe(cls, value: str | None) -> str | None:
        if value is not None and _IDENTIFIER.fullmatch(value) is None:
            raise ValueError("qualification identifiers must be opaque safe identifiers")
        return value

    @field_validator(
        "policy_digest",
        "cluster_size_vector_sha256",
        "certificate_artifact_sha256",
        "family_qualification_artifact_sha256",
        "icc_qualification_artifact_sha256",
        "source_evidence_sha256",
        "record_digest",
    )
    @classmethod
    def _digest_is_lower_sha256(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("qualification digests must be lowercase SHA-256")
        return value

    @field_validator("cluster_family_set", "grouping_keys", mode="before")
    @classmethod
    def _canonical_set(cls, value: object) -> object:
        if not isinstance(value, (list, tuple)):
            raise ValueError("qualification sets must be sequences")
        if any(type(item) is not str or not item or len(item) > 128 for item in value):
            raise ValueError("qualification set values are invalid")
        if len(value) != len(set(value)):
            raise ValueError("qualification sets must contain unique values")
        return tuple(sorted(value))

    @field_validator("cluster_size_vector", mode="before")
    @classmethod
    def _cluster_vector_is_canonical_input(cls, value: object) -> object:
        if not isinstance(value, (list, tuple)) or any(
            type(item) is not int or item <= 0 for item in value
        ):
            raise ValueError("cluster sizes must be positive integers")
        return tuple(value)

    @field_validator(
        "mean_probability_contract", "icc_upper_bound_exact", "boundary_error_alpha_exact"
    )
    @classmethod
    def _canonical_probability(cls, value: str) -> str:
        if _EXACT_FRACTION.fullmatch(value) is None:
            raise ValueError("probability contracts must be canonical exact fractions")
        fraction = Fraction(value)
        if str(fraction) != value or not 0 <= fraction <= 1:
            raise ValueError("probability contracts must be reduced and bounded")
        return value

    @model_validator(mode="after")
    def _record_is_complete_and_hash_valid(self) -> QualificationRecordV1:
        if Fraction(self.mean_probability_contract) <= 0:
            raise ValueError("mean probability must be positive")
        if not 0 < Fraction(self.boundary_error_alpha_exact) < 1:
            raise ValueError("boundary alpha must be strictly between zero and one")
        for value in (
            self.source_window_start,
            self.source_window_end,
            self.issued_at,
            self.valid_from,
            self.valid_until,
            self.revoked_at,
        ):
            if value is not None:
                _canonical_datetime(value)
        if self.source_window_start >= self.source_window_end:
            raise ValueError("qualification evidence window must be ordered")
        if self.source_window_end > self.issued_at:
            raise ValueError("qualification evidence cannot postdate issuance")
        if self.valid_from >= self.valid_until:
            raise ValueError("qualification validity window must be ordered")
        if self.revoked_at is not None and self.revoked_at < self.issued_at:
            raise ValueError("qualification revocation cannot predate issuance")
        if self.cluster_size_vector_sha256 != cluster_size_vector_digest(
            self.cluster_size_vector
        ):
            raise ValueError("cluster-size vector digest mismatch")
        if self.supersedes_qualification_id == self.qualification_id:
            raise ValueError("a qualification cannot supersede itself")
        facts: dict[str, Any] = self.model_dump(exclude={"record_digest"})
        if self.record_digest != qualification_record_digest(facts):
            raise ValueError("qualification record digest mismatch")
        return self


class QualificationApplicabilityV1(BaseModel):
    """Server-derived facts to match against one retained qualification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workload: str = Field(min_length=1, max_length=128)
    incumbent_model: str = Field(min_length=1, max_length=255)
    candidate_model: str = Field(min_length=1, max_length=255)
    policy_digest: str
    algorithm_version: str = Field(min_length=1, max_length=128)
    cluster_family_set: tuple[str, ...] = Field(min_length=1, max_length=16)
    mean_probability_contract: str = Field(max_length=128)
    icc_upper_bound_exact: str = Field(max_length=128)
    cluster_size_vector: tuple[int, ...] = Field(min_length=1, max_length=10_000)
    authorization_count_threshold: int = Field(ge=1)
    boundary_error_alpha_exact: str = Field(max_length=128)
    source_window_start: datetime
    source_window_end: datetime
    source_evidence_sha256: str

    @field_validator("cluster_family_set", mode="before")
    @classmethod
    def _canonical_families(cls, value: object) -> object:
        return QualificationRecordV1._canonical_set(value)

    @field_validator("cluster_size_vector", mode="before")
    @classmethod
    def _canonical_vector(cls, value: object) -> object:
        return QualificationRecordV1._cluster_vector_is_canonical_input(value)

    @field_validator(
        "mean_probability_contract", "icc_upper_bound_exact", "boundary_error_alpha_exact"
    )
    @classmethod
    def _canonical_probabilities(cls, value: str) -> str:
        return QualificationRecordV1._canonical_probability(value)

    @field_validator("policy_digest", "source_evidence_sha256")
    @classmethod
    def _canonical_digests(cls, value: str) -> str:
        return QualificationRecordV1._digest_is_lower_sha256(value)

    @model_validator(mode="after")
    def _window_is_complete(self) -> QualificationApplicabilityV1:
        _canonical_datetime(self.source_window_start)
        _canonical_datetime(self.source_window_end)
        if self.source_window_start >= self.source_window_end:
            raise ValueError("applicability evidence window must be ordered")
        return self


class QualificationResolution(BaseModel):
    """Fail-closed eligibility result and stable reason code."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["abstain", "eligible_for_next_evidence_stage_only"]
    reason_code: str
    qualification_id: str | None = None
    record_digest: str | None = None
    certificate_artifact_sha256: str | None = None
    public_authorization: Literal[False] = False


class QualificationConflict(RuntimeError):
    """The server-owned registry cannot safely retain requested state."""


class QualificationNotFound(LookupError):
    """The tenant-scoped qualification does not exist."""


def _scope_digest(record: QualificationRecordV1) -> str:
    identity = {
        key: getattr(record, key)
        for key in (
            "workload",
            "incumbent_model",
            "candidate_model",
            "policy_digest",
            "algorithm_version",
        )
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _utc_from_store(value: datetime | None) -> datetime | None:
    return value if value is None or value.tzinfo is not None else value.replace(tzinfo=UTC)


def _record_from_row(row: QualificationRegistryRecord) -> QualificationRecordV1:
    values = {name: getattr(row, name) for name in QualificationRecordV1.model_fields}
    for name in (
        "source_window_start",
        "source_window_end",
        "issued_at",
        "valid_from",
        "valid_until",
        "revoked_at",
    ):
        values[name] = _utc_from_store(values[name])
    return QualificationRecordV1.model_validate(values)


def _row_for_record(record: QualificationRecordV1) -> QualificationRegistryRecord:
    values = record.model_dump()
    values["cluster_family_set"] = list(record.cluster_family_set)
    values["cluster_size_vector"] = list(record.cluster_size_vector)
    values["grouping_keys"] = list(record.grouping_keys)
    for name in (
        "source_window_start",
        "source_window_end",
        "issued_at",
        "valid_from",
        "valid_until",
        "revoked_at",
    ):
        value = values[name]
        if value is not None:
            values[name] = value.astimezone(UTC).replace(tzinfo=None)
    scope_digest = _scope_digest(record)
    return QualificationRegistryRecord(
        scope_digest=scope_digest, active_scope_key=scope_digest, **values
    )


def _require_scoped(db: ScopedSession) -> str:
    if type(db) is not ScopedSession or db.scope is None:
        raise TypeError("qualification registry requires a tenant-scoped session")
    return db.scope.tenant_id


def retain_qualification(
    db: ScopedSession, record: QualificationRecordV1
) -> QualificationRecordV1:
    """Retain one server-issued immutable qualification without partial state."""
    tenant_id = _require_scoped(db)
    if record.tenant_id != tenant_id or record.revoked_at is not None:
        raise QualificationConflict("qualification ownership or lifecycle is invalid")
    key = (record.qualification_id, tenant_id)
    existing = db.get(QualificationRegistryRecord, key)
    if existing is not None:
        retained = _record_from_row(existing)
        if retained.record_digest == record.record_digest:
            return retained
        raise QualificationConflict("qualification identifier already exists")
    if record.supersedes_qualification_id is not None:
        prior = db.get(
            QualificationRegistryRecord,
            (record.supersedes_qualification_id, tenant_id),
        )
        if prior is None:
            raise QualificationConflict("superseded qualification is unavailable")
        if prior.superseded_at is not None:
            raise QualificationConflict("superseded qualification already has a successor")
        if (prior.workload, prior.incumbent_model, prior.candidate_model) != (
            record.workload,
            record.incumbent_model,
            record.candidate_model,
        ):
            raise QualificationConflict("supersession scope mismatch")
        prior_issued_at = _utc_from_store(prior.issued_at)
        assert prior_issued_at is not None
        if record.issued_at < prior_issued_at:
            raise QualificationConflict("successor issuance predates predecessor")
        prior.superseded_at = record.issued_at.astimezone(UTC).replace(tzinfo=None)
        prior.superseded_by = record.issuer
        prior.superseded_by_qualification_id = record.qualification_id
        prior.active_scope_key = None
    db.add(_row_for_record(record))
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        concurrent = db.get(QualificationRegistryRecord, key)
        if concurrent is not None and concurrent.record_digest == record.record_digest:
            return _record_from_row(concurrent)
        raise QualificationConflict("qualification scope already has an active record") from exc
    return record


def revoke_qualification(
    db: ScopedSession,
    qualification_id: str,
    *,
    revoked_by: str,
    revoked_at: datetime,
) -> QualificationRegistryRecord:
    """Idempotently append revocation state without rewriting issuance facts."""
    tenant_id = _require_scoped(db)
    if revoked_at.tzinfo is None or revoked_at.utcoffset() is None:
        raise QualificationConflict("revocation time must be timezone-aware")
    row = db.get(
        QualificationRegistryRecord,
        (qualification_id, tenant_id),
    )
    if row is None:
        raise QualificationNotFound("qualification unavailable")
    if row.revoked_at is not None:
        return row
    issued_at = _utc_from_store(row.issued_at)
    assert issued_at is not None
    if revoked_at < issued_at:
        raise QualificationConflict("revocation cannot predate issuance")
    row.revoked_at = revoked_at.astimezone(UTC).replace(tzinfo=None)
    row.revoked_by = revoked_by
    row.active_scope_key = None
    row.revocation_digest = hashlib.sha256(
        json.dumps(
            {
                "record_digest": row.record_digest,
                "revoked_at": _canonical_datetime(revoked_at),
                "revoked_by": revoked_by,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    db.commit()
    return row


def _resolution(
    reason: str, row: QualificationRegistryRecord | None = None
) -> QualificationResolution:
    return QualificationResolution(
        status="abstain",
        reason_code=reason,
        qualification_id=row.qualification_id if row is not None else None,
        record_digest=row.record_digest if row is not None else None,
        certificate_artifact_sha256=(
            row.certificate_artifact_sha256 if row is not None else None
        ),
    )


def _candidate_rows(
    db: ScopedSession,
    applicability: QualificationApplicabilityV1,
    qualification_id: str | None,
) -> list[QualificationRegistryRecord]:
    statement = select(QualificationRegistryRecord)
    if qualification_id is not None:
        statement = statement.where(
            QualificationRegistryRecord.qualification_id == qualification_id
        )
    else:
        statement = statement.where(
            QualificationRegistryRecord.workload == applicability.workload,
            QualificationRegistryRecord.incumbent_model == applicability.incumbent_model,
            QualificationRegistryRecord.candidate_model == applicability.candidate_model,
            QualificationRegistryRecord.policy_digest == applicability.policy_digest,
            QualificationRegistryRecord.algorithm_version == applicability.algorithm_version,
            QualificationRegistryRecord.active_scope_key.is_not(None),
        )
    return list(db.scalars(statement))


def resolve_qualification(
    db: ScopedSession,
    applicability: QualificationApplicabilityV1,
    *,
    qualification_id: str | None = None,
    checked_at: datetime,
    resolution_enabled: bool = True,
) -> QualificationResolution:
    """Resolve and validate retained authority without granting public authorization."""
    _require_scoped(db)
    if not resolution_enabled:
        return _resolution("qualification_resolution_disabled")
    if checked_at.tzinfo is None or checked_at.utcoffset() is None:
        return _resolution("qualification_incomplete")
    rows = _candidate_rows(db, applicability, qualification_id)
    if not rows:
        return _resolution("qualification_absent")
    if len(rows) != 1:
        return _resolution("qualification_ambiguous")
    row = rows[0]
    superseded_at = _utc_from_store(row.superseded_at)
    if superseded_at is not None and checked_at >= superseded_at:
        return _resolution("qualification_superseded", row)
    revoked_at = _utc_from_store(row.revoked_at)
    if revoked_at is not None and checked_at >= revoked_at:
        return _resolution("qualification_revoked", row)
    valid_from = _utc_from_store(row.valid_from)
    valid_until = _utc_from_store(row.valid_until)
    assert valid_from is not None and valid_until is not None
    if checked_at < valid_from:
        return _resolution("qualification_not_yet_valid", row)
    if checked_at >= valid_until:
        return _resolution("qualification_expired", row)
    try:
        record = _record_from_row(row)
    except ValidationError as exc:
        reason = (
            "qualification_hash_invalid"
            if "digest mismatch" in str(exc)
            else "qualification_malformed"
        )
        return _resolution(reason, row)
    checks = (
        (record.workload != applicability.workload, "qualification_workload_scope_mismatch"),
        (
            (record.incumbent_model, record.candidate_model)
            != (applicability.incumbent_model, applicability.candidate_model),
            "qualification_model_pair_scope_mismatch",
        ),
        (record.policy_digest != applicability.policy_digest, "qualification_policy_scope_mismatch"),
        (
            record.algorithm_version != applicability.algorithm_version,
            "qualification_algorithm_scope_mismatch",
        ),
        (
            not set(applicability.cluster_family_set) <= _SUPPORTED_FAMILIES,
            "qualification_unsupported_family",
        ),
        (
            not set(record.cluster_family_set) <= _SUPPORTED_FAMILIES,
            "qualification_unsupported_family",
        ),
        (
            not set(applicability.cluster_family_set) <= set(record.cluster_family_set),
            "qualification_family_set_mismatch",
        ),
        (
            record.mean_probability_contract != applicability.mean_probability_contract,
            "qualification_mean_contract_mismatch",
        ),
        (
            Fraction(applicability.icc_upper_bound_exact) > Fraction(record.icc_upper_bound_exact),
            "qualification_icc_bound_exceeded",
        ),
        (
            record.cluster_size_vector != applicability.cluster_size_vector,
            "qualification_cluster_vector_mismatch",
        ),
        (
            record.authorization_count_threshold
            != applicability.authorization_count_threshold,
            "qualification_threshold_mismatch",
        ),
        (
            record.boundary_error_alpha_exact != applicability.boundary_error_alpha_exact,
            "qualification_alpha_mismatch",
        ),
        (
            (
                record.source_window_start,
                record.source_window_end,
                record.source_evidence_sha256,
            )
            != (
                applicability.source_window_start,
                applicability.source_window_end,
                applicability.source_evidence_sha256,
            ),
            "qualification_evidence_window_mismatch",
        ),
    )
    for failed, reason in checks:
        if failed:
            return _resolution(reason, row)
    return QualificationResolution(
        status="eligible_for_next_evidence_stage_only",
        reason_code="qualification_applicable",
        qualification_id=row.qualification_id,
        record_digest=row.record_digest,
        certificate_artifact_sha256=row.certificate_artifact_sha256,
    )


def erase_qualifications_for_workload(db: ScopedSession, workload: str) -> int:
    """Delete all qualification and lifecycle lineage for one tenant workload."""
    _require_scoped(db)
    rows = list(
        db.scalars(
            select(QualificationRegistryRecord).where(
                QualificationRegistryRecord.workload == workload
            )
        )
    )
    db.execute(
        delete(QualificationRegistryRecord).where(
            QualificationRegistryRecord.workload == workload
        )
    )
    db.commit()
    return len(rows)
