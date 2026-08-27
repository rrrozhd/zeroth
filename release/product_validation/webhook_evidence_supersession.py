"""Normalize sealed Playwright webhook proofs without changing their claims.

Some early Playwright result records correctly listed normalized evidence paths
while the sealed bundle retained the same bytes below ``indexed/``.  This
module creates a new immutable bundle by verifying the original seal and exact
criterion IDs, copying only the artifact-map bytes, and preserving those exact
IDs.  It never translates aliases.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from release.live_evaluation.evidence import AcceptanceCriterion, EvidenceStore

from .evidence_mapper import (
    ProductEvidenceSource,
    _file_failure,
    _load_source,
    _safe_relative,
)


class WebhookEvidenceSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    campaign: str
    bucket: str
    root: str
    record: str
    criterion_ids: tuple[str, ...] = Field(min_length=1)

    @field_validator("campaign", "root")
    @classmethod
    def _safe_slug(cls, value: str) -> str:
        return _safe_relative(value, single_segment=True)

    @field_validator("bucket", "record")
    @classmethod
    def _safe_path(cls, value: str) -> str:
        return _safe_relative(value)

    @model_validator(mode="after")
    def _unique_exact_criteria(self) -> WebhookEvidenceSource:
        if len(self.criterion_ids) != len(set(self.criterion_ids)):
            raise ValueError("webhook source criterion IDs must be unique")
        if Path(self.record).name != "results.json":
            raise ValueError("webhook source must be a completed results.json record")
        return self


@dataclass(frozen=True, slots=True)
class _NormalizedArtifact:
    source: Path
    destination: Path


@dataclass(frozen=True, slots=True)
class _ValidatedInput:
    source: WebhookEvidenceSource
    root: Path
    record: Path
    checksum: Path
    checksum_sha256: str
    criteria: dict[str, tuple[str, ...]]
    artifacts: tuple[_NormalizedArtifact, ...]


def _safe_artifact_path(value: object, *, label: str) -> Path:
    if not isinstance(value, str):
        raise RuntimeError(f"invalid {label} artifact path")
    try:
        return Path(_safe_relative(value))
    except ValueError as exc:
        raise RuntimeError(f"unsafe {label} artifact path") from exc


def _validate_source(
    evidence_base: Path,
    requested: WebhookEvidenceSource,
) -> _ValidatedInput:
    config = ProductEvidenceSource(
        campaign=requested.campaign,
        bucket=requested.bucket,
        root=requested.root,
        record=requested.record,
        record_kind="completed_results",
    )
    try:
        loaded = _load_source(evidence_base, config)
    except Exception as exc:
        raise RuntimeError(f"source checksum or acceptance validation failed: {exc}") from exc

    record = loaded.root / requested.record
    document = json.loads(record.read_text(encoding="utf-8"))
    raw_artifacts = document.get("artifacts")
    if not isinstance(raw_artifacts, list):
        raise RuntimeError("completed source has no artifact map")
    record_parent = Path(requested.record).parent
    by_destination: dict[str, Path] = {}
    for row in raw_artifacts:
        if not isinstance(row, dict):
            raise RuntimeError("invalid source artifact map")
        source_relative = record_parent / _safe_artifact_path(
            row.get("source"), label="source"
        )
        destination_relative = _safe_artifact_path(
            row.get("destination"), label="destination"
        )
        destination_text = destination_relative.as_posix()
        if destination_text in by_destination:
            raise RuntimeError(f"duplicate source artifact destination: {destination_text}")
        source_file = loaded.root / source_relative
        if (
            source_file.is_symlink()
            or not source_file.is_file()
            or source_relative.as_posix() not in loaded.covered_files
        ):
            raise RuntimeError(f"source artifact is not checksum-sealed: {source_relative}")
        by_destination[destination_text] = source_file

    criteria: dict[str, tuple[str, ...]] = {}
    artifacts: dict[str, _NormalizedArtifact] = {}
    for criterion_id in requested.criterion_ids:
        assertion = loaded.assertions.get(criterion_id)
        if assertion is None or assertion.status != "pass":
            raise RuntimeError(f"missing exact passing criterion: {criterion_id}")
        if not assertion.evidence:
            raise RuntimeError(f"exact criterion has no evidence: {criterion_id}")
        normalized: list[str] = []
        for reference in assertion.evidence:
            path_text, separator, _fragment = reference.partition("#")
            if separator:
                raise RuntimeError("fragment evidence cannot be normalized from artifact map")
            source_file = by_destination.get(path_text)
            if source_file is None:
                raise RuntimeError(f"criterion evidence is absent from artifact map: {reference}")
            original_destination = Path(path_text)
            destination = (
                Path(original_destination.parts[0])
                / requested.label
                / Path(*original_destination.parts[1:])
            )
            destination_text = destination.as_posix()
            artifacts.setdefault(
                destination_text,
                _NormalizedArtifact(source_file, destination),
            )
            normalized.append(destination_text)
        criteria[criterion_id] = tuple(normalized)

    checksum = loaded.root / "SHA256SUMS"
    return _ValidatedInput(
        requested,
        loaded.root,
        record,
        checksum,
        hashlib.sha256(checksum.read_bytes()).hexdigest(),
        criteria,
        tuple(artifacts.values()),
    )


def build_webhook_evidence_supersession(
    *,
    evidence_base: Path,
    destination: Path,
    sources: tuple[WebhookEvidenceSource, ...],
) -> Path:
    """Create a sealed exact-ID supersession from immutable source bytes."""
    if not sources:
        raise ValueError("at least one webhook evidence source is required")
    labels = [source.label for source in sources]
    if len(labels) != len(set(labels)):
        raise ValueError("webhook evidence source labels must be unique")
    if destination.exists():
        raise FileExistsError(destination)

    validated = tuple(_validate_source(evidence_base, source) for source in sources)
    merged: dict[str, list[str]] = {}
    for item in validated:
        for criterion_id, references in item.criteria.items():
            values = merged.setdefault(criterion_id, [])
            for reference in references:
                if reference not in values:
                    values.append(reference)

    store = EvidenceStore(destination)
    store.write_manifest(
        {
            "kind": "webhook_exact_evidence_supersession",
            "schema_version": 1,
            "sources": [
                {
                    "campaign": item.source.campaign,
                    "bucket": item.source.bucket,
                    "root": item.source.root,
                    "record": item.source.record,
                    "criterion_ids": list(item.source.criterion_ids),
                    "checksum_manifest_sha256": item.checksum_sha256,
                }
                for item in validated
            ],
        }
    )
    for item in validated:
        store.ingest_artifact(
            item.record,
            Path("reconciliation") / f"{item.source.label}-source-results.json",
        )
        store.ingest_artifact(
            item.checksum,
            Path("reconciliation") / f"{item.source.label}-source-sha256sums.txt",
        )
        for artifact in item.artifacts:
            store.ingest_artifact(artifact.source, artifact.destination)

    criteria = tuple(
        AcceptanceCriterion(criterion_id, "pass", tuple(references))
        for criterion_id, references in sorted(merged.items())
    )
    store.finalize_bundle(
        acceptance=criteria,
        report_markdown=(
            "# Exact webhook evidence supersession\n\n"
            "Every source checksum and recursive secret scan passed. Exact criterion IDs "
            "were retained verbatim; normalized proof bytes were copied through each "
            "source result record's artifact map. No runtime or provider call was made.\n"
        ),
    )
    return destination


_SQLITE_ATOMICITY_CASES = (
    "subscription-create-rollback",
    "subscription-deactivate-rollback",
    "dead-letter-replay-rollback",
    "delivery-enqueue-rollback",
    "delivery-fanout-rollback",
    "delivery-unsigned-fail-closed",
    "delivery-chain-valid",
    "delivery-chain-head-rollback",
    "delivery-delivered-rollback",
    "delivery-failed-rollback",
    "delivery-dead-letter-rollback",
    "delivery-lost-fence",
    "subscription-tenant-collision",
    "subscription-audit-sanitization",
    "delivery-failure-audit-sanitization",
    "delivery-dead-letter-linkage",
)
_POSTGRES_ATOMICITY_CASES = (
    "subscription-create-rollback",
    "subscription-deactivate-rollback",
    "dead-letter-replay-rollback",
)
_ATOMICITY_SEMANTICS = {
    "sqlite": {
        "source_criterion_id": "WEBHOOKS-TRANSACTIONAL-STATE-AND-AUDIT-D013",
        "product_criterion_id": "webhooks.transactional-state-and-audit-sqlite",
        "case_names": _SQLITE_ATOMICITY_CASES,
        "postgres_proven": False,
    },
    "postgres": {
        "source_criterion_id": "WEBHOOKS-POSTGRES-ATOMICITY-D013",
        "product_criterion_id": "webhooks.transactional-state-and-audit-postgres",
        "case_names": _POSTGRES_ATOMICITY_CASES,
        "postgres_proven": True,
    },
}


class WebhookAtomicityEvidenceSource(BaseModel):
    """One sealed D-013 source with an explicitly reviewed backend meaning."""

    model_config = ConfigDict(extra="forbid")

    campaign: str
    bucket: str
    root: str
    backend: Literal["sqlite", "postgres"]

    @field_validator("campaign", "root")
    @classmethod
    def _atomicity_safe_slug(cls, value: str) -> str:
        return _safe_relative(value, single_segment=True)

    @field_validator("bucket")
    @classmethod
    def _atomicity_safe_path(cls, value: str) -> str:
        return _safe_relative(value)


@dataclass(frozen=True, slots=True)
class _ValidatedAtomicityInput:
    requested: WebhookAtomicityEvidenceSource
    root: Path
    product_criterion_id: str
    evidence: tuple[str, ...]
    proof_files: tuple[Path, ...]
    checksum_sha256: str


def _validate_atomicity_source(
    evidence_base: Path,
    requested: WebhookAtomicityEvidenceSource,
) -> _ValidatedAtomicityInput:
    semantics = _ATOMICITY_SEMANTICS[requested.backend]
    config = ProductEvidenceSource(
        campaign=requested.campaign,
        bucket=requested.bucket,
        root=requested.root,
        record="acceptance.json",
        record_kind="acceptance",
    )
    try:
        loaded = _load_source(evidence_base, config)
    except Exception as exc:
        raise RuntimeError(f"atomicity source validation failed: {exc}") from exc

    source_criterion_id = str(semantics["source_criterion_id"])
    assertion = loaded.assertions.get(source_criterion_id)
    if assertion is None or assertion.status != "pass":
        raise RuntimeError(f"missing exact passing atomicity criterion: {source_criterion_id}")
    failures = [
        failure
        for reference in assertion.evidence
        if (failure := _file_failure(loaded, reference)) is not None
    ]
    if failures:
        raise RuntimeError(f"atomicity criterion evidence failed validation: {failures}")

    manifest = json.loads((loaded.root / "manifest.json").read_text(encoding="utf-8"))
    expected_cases = tuple(str(value) for value in semantics["case_names"])
    if (
        manifest.get("backend") != requested.backend
        or manifest.get("postgres_proven") is not semantics["postgres_proven"]
    ):
        raise RuntimeError("atomicity manifest does not match reviewed backend semantics")

    command_references = [
        reference for reference in assertion.evidence if reference.startswith("commands/")
    ]
    command_names: list[str] = []
    for reference in command_references:
        document = json.loads((loaded.root / reference).read_text(encoding="utf-8"))
        if document.get("exit_code") != 0 or not isinstance(document.get("name"), str):
            raise RuntimeError("atomicity command did not pass")
        command_names.append(document["name"])
    if tuple(command_names) != expected_cases:
        raise RuntimeError("atomicity semantic case matrix is incomplete or reordered")
    if manifest.get("required_case_count") != len(expected_cases):
        raise RuntimeError("atomicity semantic case matrix count is inconsistent")
    if not any(reference.startswith("events.ndjson#") for reference in assertion.evidence):
        raise RuntimeError("atomicity source lacks a correlated event record")

    normalized: list[str] = []
    proof_files: list[Path] = []
    for reference in assertion.evidence:
        path_text = reference.partition("#")[0]
        destination = (
            Path("reconciliation") / requested.backend / "proofs" / path_text
        )
        normalized.append(destination.as_posix())
        source_file = loaded.root / path_text
        if source_file not in proof_files:
            proof_files.append(source_file)

    checksum = loaded.root / "SHA256SUMS"
    return _ValidatedAtomicityInput(
        requested=requested,
        root=loaded.root,
        product_criterion_id=str(semantics["product_criterion_id"]),
        evidence=tuple(normalized),
        proof_files=tuple(proof_files),
        checksum_sha256=hashlib.sha256(checksum.read_bytes()).hexdigest(),
    )


def build_webhook_atomicity_product_supersession(
    *,
    evidence_base: Path,
    destination: Path,
    sources: tuple[WebhookAtomicityEvidenceSource, ...],
) -> Path:
    """Seal product-ID assertions only after exact D-013 semantic validation."""
    backends = [source.backend for source in sources]
    if len(backends) != len(set(backends)):
        raise ValueError("atomicity source backends must be unique")
    if destination.exists():
        raise FileExistsError(destination)

    validated = tuple(_validate_atomicity_source(evidence_base, source) for source in sources)
    if set(backends) != set(_ATOMICITY_SEMANTICS):
        raise ValueError("both SQLite and PostgreSQL atomicity sources are required")

    store = EvidenceStore(destination)
    store.write_manifest(
        {
            "kind": "webhook_atomicity_product_semantic_supersession",
            "schema_version": 1,
            "sources": [
                {
                    "backend": item.requested.backend,
                    "campaign": item.requested.campaign,
                    "bucket": item.requested.bucket,
                    "root": item.requested.root,
                    "source_criterion_id": _ATOMICITY_SEMANTICS[item.requested.backend][
                        "source_criterion_id"
                    ],
                    "product_criterion_id": item.product_criterion_id,
                    "checksum_manifest_sha256": item.checksum_sha256,
                }
                for item in validated
            ],
        }
    )
    for item in validated:
        backend = item.requested.backend
        store.ingest_artifact(
            item.root / "acceptance.json",
            Path("reconciliation") / backend / "source-acceptance.json",
        )
        store.ingest_artifact(
            item.root / "manifest.json",
            Path("reconciliation") / backend / "source-manifest.json",
        )
        store.ingest_artifact(
            item.root / "SHA256SUMS",
            Path("reconciliation") / backend / "source-sha256sums.txt",
        )
        for source_file in item.proof_files:
            relative = source_file.relative_to(item.root)
            store.ingest_artifact(
                source_file,
                Path("reconciliation") / backend / "proofs" / relative,
            )

    store.finalize_bundle(
        acceptance=tuple(
            AcceptanceCriterion(item.product_criterion_id, "pass", item.evidence)
            for item in sorted(validated, key=lambda value: value.product_criterion_id)
        ),
        report_markdown=(
            "# Webhook atomicity product-criterion supersession\n\n"
            "The product criteria are backend-qualified statements of the same D-013 "
            "transaction boundary proved by the sealed sources: domain mutation, signed "
            "audit row, and audit-chain head commit or roll back together. SQLite carries "
            "the complete 16-case state/audit matrix; PostgreSQL carries the three exact "
            "operator mutations (create, deactivate, replay) required by D-013. Exact "
            "case inventories, exit codes, source seals, and recursive secret scans were "
            "validated before these product IDs were emitted. No provider, runtime, or "
            "database was called.\n"
        ),
    )
    return destination
