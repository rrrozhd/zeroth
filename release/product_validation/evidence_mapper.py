"""Fail-closed evidence resolution for the published product validation index.

The product index is a presentation ledger, not an authority for acceptance.
This module only validates its claims against explicitly mapped, immutable
campaign evidence.  It intentionally does not infer aliases from similar
criterion names or mutate capability statuses.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from release.live_evaluation.evidence import EvidenceStore

_SAFE_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _safe_relative(value: str, *, single_segment: bool = False) -> str:
    path = Path(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
        or any(not part for part in path.parts)
        or (single_segment and len(path.parts) != 1)
    ):
        raise ValueError("value must be a safe relative path")
    if single_segment and not _SAFE_SLUG.fullmatch(value):
        raise ValueError("value must be a safe path slug")
    return value


class ProductEvidenceSource(BaseModel):
    """One immutable source, qualified below the external evaluations root."""

    model_config = ConfigDict(extra="forbid")

    campaign: str
    bucket: str
    root: str
    record: str
    record_kind: Literal["acceptance", "completed_results"]

    @field_validator("campaign", "root")
    @classmethod
    def _safe_slug(cls, value: str) -> str:
        return _safe_relative(value, single_segment=True)

    @field_validator("bucket", "record")
    @classmethod
    def _safe_path(cls, value: str) -> str:
        return _safe_relative(value)

    @model_validator(mode="after")
    def _record_matches_semantics(self) -> ProductEvidenceSource:
        expected = "acceptance.json" if self.record_kind == "acceptance" else "results.json"
        if Path(self.record).name != expected:
            raise ValueError(f"{self.record_kind} source record must end in {expected}")
        return self

    @property
    def relative_root(self) -> Path:
        return Path(self.campaign) / self.bucket / self.root


class ProductEvidenceMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criterion_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    source_criterion_ids: tuple[str, ...] = Field(min_length=1)
    files: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_safe_proofs(self) -> ProductEvidenceMapping:
        if len(self.source_criterion_ids) != len(set(self.source_criterion_ids)):
            raise ValueError("source criterion proofs must be unique")
        if len(self.files) != len(set(self.files)):
            raise ValueError("required evidence files must be unique")
        for value in self.files:
            path_text = value.partition("#")[0]
            _safe_relative(path_text)
        return self


class ProductEvidenceSourceMap(BaseModel):
    """Versioned, explicit product-criterion to accepted-source map."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    catalog_id: str = Field(min_length=1)
    sources: dict[str, ProductEvidenceSource]
    mappings: tuple[ProductEvidenceMapping, ...] = ()
    unmapped: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_references(self) -> ProductEvidenceSourceMap:
        targets = [mapping.criterion_id for mapping in self.mappings]
        if len(targets) != len(set(targets)):
            raise ValueError("product evidence mappings must have unique criterion targets")
        overlap = set(targets).intersection(self.unmapped)
        if overlap:
            raise ValueError(f"criterion cannot be mapped and unmapped: {sorted(overlap)}")
        unknown_sources = {mapping.source for mapping in self.mappings} - set(self.sources)
        if unknown_sources:
            raise ValueError(f"mapping references unknown source: {sorted(unknown_sources)}")
        if any(not reason.strip() for reason in self.unmapped.values()):
            raise ValueError("unmapped evidence reasons must be non-empty")
        return self


ProductEvidenceStatus = Literal["pass", "fail", "unmapped"]


@dataclass(frozen=True, slots=True)
class ProductEvidenceAuditEntry:
    capability_id: str
    capability_status: str
    criterion_id: str
    status: ProductEvidenceStatus
    source_location: str | None = None
    source_criterion_ids: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    note: str | None = None


@dataclass(frozen=True, slots=True)
class ProductEvidenceAuditResult:
    entries: tuple[ProductEvidenceAuditEntry, ...]

    @property
    def counts(self) -> dict[str, int]:
        return {
            status: sum(entry.status == status for entry in self.entries)
            for status in ("pass", "fail", "unmapped")
        }

    @property
    def complete(self) -> bool:
        return bool(self.entries) and all(entry.status == "pass" for entry in self.entries)

    @property
    def declared_passes_valid(self) -> bool:
        return all(
            entry.status == "pass"
            for entry in self.entries
            if entry.capability_status == "pass"
        )


@dataclass(frozen=True, slots=True)
class _Assertion:
    status: str | None
    evidence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ValidatedSource:
    root: Path
    location: str
    covered_files: frozenset[str]
    assertions: dict[str, _Assertion]


def _verify_checksums(root: Path) -> frozenset[str]:
    manifest = root / "SHA256SUMS"
    if not manifest.is_file():
        raise ValueError("missing checksum manifest")
    declared: dict[str, str] = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2 or not _SHA256.fullmatch(parts[0]):
            raise ValueError("malformed checksum manifest")
        digest, relative_text = parts
        relative = relative_text.lstrip("* ")
        _safe_relative(relative)
        if relative == "SHA256SUMS" or relative in declared:
            raise ValueError("invalid or duplicate checksum path")
        declared[relative] = digest

    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != "SHA256SUMS"
    }
    if set(declared) != actual:
        raise ValueError("checksum inventory mismatch")
    for relative, expected in declared.items():
        actual_digest = hashlib.sha256((root / relative).read_bytes()).hexdigest()
        if actual_digest != expected:
            raise ValueError(f"checksum mismatch: {relative}")
    return frozenset(declared)


def _load_source(evidence_base: Path, source: ProductEvidenceSource) -> _ValidatedSource:
    base = evidence_base.expanduser().resolve(strict=True)
    unresolved = base / source.relative_root
    if unresolved.is_symlink():
        raise ValueError("source evidence root cannot be a symlink")
    root = unresolved.resolve(strict=True)
    try:
        root.relative_to(base)
    except ValueError as exc:
        raise ValueError("source evidence root escapes the evidence base") from exc

    covered = _verify_checksums(root)
    try:
        EvidenceStore(root).scan_recursive()
    except Exception as exc:
        raise ValueError(f"source secret scan failed: {exc}") from exc

    record_path = root / source.record
    record_relative = Path(source.record).as_posix()
    if record_relative not in covered or not record_path.is_file():
        raise ValueError(f"source record is not checksum-sealed: {source.record}")
    try:
        document = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid source record: {source.record}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("criteria"), list):
        raise ValueError(f"invalid source record: {source.record}")
    if source.record_kind == "completed_results" and document.get("completed") is not True:
        raise ValueError("completed_results source requires completed=true")

    assertions: dict[str, _Assertion] = {}
    for row in document["criteria"]:
        if not isinstance(row, dict) or not isinstance(row.get("criterion_id"), str):
            raise ValueError("source criterion requires exact criterion_id")
        criterion_id = row["criterion_id"]
        if criterion_id in assertions:
            raise ValueError(f"duplicate source criterion_id: {criterion_id}")
        raw_evidence = row.get("evidence", [])
        if not isinstance(raw_evidence, list) or not all(
            isinstance(value, str) and value for value in raw_evidence
        ):
            raise ValueError(f"invalid evidence references for {criterion_id}")
        assertions[criterion_id] = _Assertion(
            status=row.get("status") if isinstance(row.get("status"), str) else None,
            evidence=tuple(raw_evidence),
        )

    location = (source.relative_root / source.record).as_posix()
    return _ValidatedSource(root, location, covered, assertions)


def _file_failure(source: _ValidatedSource, reference: str) -> str | None:
    path_text = reference.partition("#")[0]
    try:
        _safe_relative(path_text)
    except ValueError:
        return f"{reference}=unsafe"
    target = source.root / path_text
    if not target.is_file():
        return f"{reference}=missing"
    if path_text != "SHA256SUMS" and path_text not in source.covered_files:
        return f"{reference}=not-checksum-sealed"
    return None


def audit_product_evidence(
    index: dict[str, object],
    *,
    evidence_base: Path,
    source_map: ProductEvidenceSourceMap,
) -> ProductEvidenceAuditResult:
    """Resolve every product-index assertion without changing the index."""
    if index.get("catalog_id") != source_map.catalog_id:
        raise ValueError("product evidence source-map catalog_id does not match index")
    raw_entries = index.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError("product evidence index has no entries")

    index_criteria: set[str] = set()
    normalized: list[tuple[str, str, tuple[str, ...]]] = []
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            raise ValueError("invalid product evidence index entry")
        capability_id = raw_entry.get("capability_id")
        capability_status = raw_entry.get("status")
        raw_criteria = raw_entry.get("evidence_criteria")
        if (
            not isinstance(capability_id, str)
            or not isinstance(capability_status, str)
            or not isinstance(raw_criteria, list)
            or not all(isinstance(value, str) and value for value in raw_criteria)
        ):
            raise ValueError("invalid product evidence index entry")
        if len(raw_criteria) != len(set(raw_criteria)):
            raise ValueError(f"duplicate product criterion in capability: {capability_id}")
        criteria = tuple(raw_criteria)
        index_criteria.update(criteria)
        normalized.append((capability_id, capability_status, criteria))

    declared = {mapping.criterion_id for mapping in source_map.mappings} | set(
        source_map.unmapped
    )
    unknown = declared - index_criteria
    if unknown:
        raise ValueError(f"source map references unknown product criterion: {sorted(unknown)}")

    mappings = {mapping.criterion_id: mapping for mapping in source_map.mappings}
    loaded: dict[str, _ValidatedSource | Exception] = {}
    output: list[ProductEvidenceAuditEntry] = []
    for capability_id, capability_status, criteria in normalized:
        for criterion_id in criteria:
            mapping = mappings.get(criterion_id)
            if mapping is None:
                output.append(
                    ProductEvidenceAuditEntry(
                        capability_id,
                        capability_status,
                        criterion_id,
                        "unmapped",
                        note=source_map.unmapped.get(
                            criterion_id, "no explicit accepted-source mapping"
                        ),
                    )
                )
                continue

            source_config = source_map.sources[mapping.source]
            if mapping.source not in loaded:
                try:
                    loaded[mapping.source] = _load_source(evidence_base, source_config)
                except Exception as exc:
                    loaded[mapping.source] = exc
            source = loaded[mapping.source]
            if isinstance(source, Exception):
                location = (source_config.relative_root / source_config.record).as_posix()
                output.append(
                    ProductEvidenceAuditEntry(
                        capability_id,
                        capability_status,
                        criterion_id,
                        "fail",
                        source_location=location,
                        source_criterion_ids=mapping.source_criterion_ids,
                        files=mapping.files,
                        note=f"source evidence failed validation: {source}",
                    )
                )
                continue

            failures: list[str] = []
            assertions: list[_Assertion] = []
            for source_criterion_id in mapping.source_criterion_ids:
                assertion = source.assertions.get(source_criterion_id)
                if assertion is None:
                    failures.append(f"{source_criterion_id}=missing")
                elif assertion.status != "pass":
                    failures.append(f"{source_criterion_id}={assertion.status or 'invalid'}")
                else:
                    assertions.append(assertion)
            for reference in (
                *mapping.files,
                *(value for assertion in assertions for value in assertion.evidence),
            ):
                if (failure := _file_failure(source, reference)) and failure not in failures:
                    failures.append(failure)

            if failures:
                output.append(
                    ProductEvidenceAuditEntry(
                        capability_id,
                        capability_status,
                        criterion_id,
                        "fail",
                        source_location=source.location,
                        source_criterion_ids=mapping.source_criterion_ids,
                        files=mapping.files,
                        note="source proof is not an accepted pass: " + ", ".join(failures),
                    )
                )
                continue
            output.append(
                ProductEvidenceAuditEntry(
                    capability_id,
                    capability_status,
                    criterion_id,
                    "pass",
                    source_location=source.location,
                    source_criterion_ids=mapping.source_criterion_ids,
                    files=mapping.files,
                )
            )

    return ProductEvidenceAuditResult(tuple(output))
