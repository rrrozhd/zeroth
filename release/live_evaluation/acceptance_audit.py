"""Conservative cross-root audit for the immutable campaign acceptance catalog.

This module never upgrades a criterion from similarly named evidence. Every
pass must be declared in a versioned source map and must resolve to an accepted
passing assertion in a secret-clean evidence root. It produces an interim gap
audit, not the campaign's final ``acceptance.json``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .evidence import AcceptanceCriterion, AcceptanceStatus, EvidenceStore


class AcceptanceSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    record: Literal["results.json", "acceptance.json"]


class AcceptanceMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criterion_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    source_criterion_id: str | None = Field(default=None, min_length=1)
    source_criterion_ids: tuple[str, ...] = ()
    files: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_proofs(self) -> AcceptanceMapping:
        if self.source_criterion_id is not None and self.source_criterion_ids:
            raise ValueError("use singular or plural source criterion proof, not both")
        if not self.assertion_ids and not self.files:
            raise ValueError("acceptance mapping requires an assertion or file proof")
        if len(self.assertion_ids) != len(set(self.assertion_ids)):
            raise ValueError("acceptance assertion proofs must be unique")
        if len(self.files) != len(set(self.files)):
            raise ValueError("acceptance file proofs must be unique")
        for value in self.files:
            path = Path(value)
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise ValueError("acceptance file proof must be a safe relative path")
        return self

    @property
    def assertion_ids(self) -> tuple[str, ...]:
        if self.source_criterion_id is not None:
            return (self.source_criterion_id,)
        return self.source_criterion_ids


class AcceptanceSourceMap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    sources: dict[str, AcceptanceSource]
    mappings: tuple[AcceptanceMapping, ...] = ()
    blocked: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_references(self) -> AcceptanceSourceMap:
        targets = [item.criterion_id for item in self.mappings]
        if len(targets) != len(set(targets)):
            raise ValueError("acceptance mappings must have unique criterion targets")
        overlap = set(targets).intersection(self.blocked)
        if overlap:
            raise ValueError(f"criterion cannot be both mapped and blocked: {sorted(overlap)}")
        unknown_sources = {item.source for item in self.mappings} - set(self.sources)
        if unknown_sources:
            raise ValueError(
                f"acceptance mapping references unknown source: {sorted(unknown_sources)}"
            )
        if any(not reason.strip() for reason in self.blocked.values()):
            raise ValueError("blocked acceptance reasons must be non-empty")
        return self


@dataclass(frozen=True, slots=True)
class AcceptanceAuditEntry:
    criterion_id: str
    status: AcceptanceStatus
    source_root: str | None = None
    source_criterion_ids: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    note: str | None = None

    @property
    def source_criterion_id(self) -> str | None:
        return self.source_criterion_ids[0] if len(self.source_criterion_ids) == 1 else None


@dataclass(frozen=True, slots=True)
class AcceptanceAuditResult:
    criteria: tuple[AcceptanceAuditEntry, ...]

    @property
    def counts(self) -> dict[str, int]:
        return {
            status: sum(item.status == status for item in self.criteria)
            for status in ("pass", "fail", "blocked", "not_run")
        }


def write_gap_audit(
    result: AcceptanceAuditResult,
    *,
    output_root: Path,
) -> tuple[Path, Path]:
    """Persist an append-only interim audit without claiming final acceptance."""
    store = EvidenceStore(output_root)
    criteria = [
        {
            "criterion_id": item.criterion_id,
            "status": item.status,
            "source_root": item.source_root,
            "source_criterion_ids": list(item.source_criterion_ids),
            "files": list(item.files),
            "note": item.note,
        }
        for item in result.criteria
    ]
    document = {
        "schema_version": 1,
        "kind": "interim_acceptance_gap_audit",
        "counts": result.counts,
        "criteria": criteria,
    }
    json_path = store._write_exclusive(Path("gap-audit.json"), document)
    lines = [
        "# Interim acceptance gap audit",
        "",
        "This is not final campaign acceptance. Unmapped and blocked criteria remain open.",
        "",
        "| Status | Count |",
        "| --- | ---: |",
        *(
            f"| {status} | {result.counts[status]} |"
            for status in ("pass", "fail", "blocked", "not_run")
        ),
    ]
    for status in ("fail", "blocked", "not_run"):
        rows = [item for item in result.criteria if item.status == status]
        if not rows:
            continue
        lines.extend(("", f"## {status.replace('_', ' ').title()}", ""))
        lines.extend(
            f"- `{item.criterion_id}` — {item.note or 'no supporting evidence'}" for item in rows
        )
    report_path = store.write_report("\n".join(lines) + "\n")
    store.scan_recursive()
    return json_path, report_path


@dataclass(frozen=True, slots=True)
class _SourceAssertion:
    accepted: bool
    status: str | None
    note: str | None


def _load_source_assertions(
    evidence_root: Path, source: AcceptanceSource
) -> dict[str, _SourceAssertion]:
    root = evidence_root / source.root
    record = root / source.record
    if not record.is_file():
        raise ValueError(f"missing source record: {source.root}/{source.record}")
    EvidenceStore(root).scan_recursive()
    document = json.loads(record.read_text())
    if not isinstance(document, dict) or not isinstance(document.get("criteria"), list):
        raise ValueError(f"invalid source record: {source.root}/{source.record}")
    completed = source.record == "acceptance.json" or document.get("completed") is True
    assertions: dict[str, _SourceAssertion] = {}
    for row in document["criteria"]:
        if not isinstance(row, dict) or not isinstance(row.get("criterion_id"), str):
            raise ValueError(f"invalid source criterion: {source.root}/{source.record}")
        criterion_id = row["criterion_id"]
        if criterion_id in assertions:
            raise ValueError(f"duplicate source criterion: {source.root}:{criterion_id}")
        status = row.get("status") if isinstance(row.get("status"), str) else None
        note = row.get("note") if isinstance(row.get("note"), str) else None
        assertions[criterion_id] = _SourceAssertion(
            accepted=completed and status == "pass",
            status=status,
            note=note,
        )
    return assertions


def audit_acceptance(
    catalog: tuple[AcceptanceCriterion, ...],
    *,
    evidence_root: Path,
    source_map: AcceptanceSourceMap,
) -> AcceptanceAuditResult:
    """Audit explicit mappings against current external evidence.

    Source failures are recorded as criterion failures so a broken or changed
    evidence root cannot silently degrade to ``not_run``.
    """
    catalog_ids = [item.criterion_id for item in catalog]
    if len(catalog_ids) != len(set(catalog_ids)):
        raise ValueError("acceptance catalog contains duplicate criteria")
    known = set(catalog_ids)
    declared = {item.criterion_id for item in source_map.mappings} | set(source_map.blocked)
    unknown = declared - known
    if unknown:
        raise ValueError(f"source map references unknown criterion: {sorted(unknown)}")

    mappings = {item.criterion_id: item for item in source_map.mappings}
    loaded: dict[str, dict[str, _SourceAssertion] | Exception] = {}
    entries: list[AcceptanceAuditEntry] = []
    for criterion_id in catalog_ids:
        if reason := source_map.blocked.get(criterion_id):
            entries.append(AcceptanceAuditEntry(criterion_id, "blocked", note=reason))
            continue
        mapping = mappings.get(criterion_id)
        if mapping is None:
            entries.append(
                AcceptanceAuditEntry(
                    criterion_id,
                    "not_run",
                    note="no explicit accepted-source mapping",
                )
            )
            continue
        source = source_map.sources[mapping.source]
        if mapping.source not in loaded:
            try:
                loaded[mapping.source] = _load_source_assertions(evidence_root, source)
            except Exception as exc:  # evidence failures must become durable audit failures
                loaded[mapping.source] = exc
        assertions = loaded[mapping.source]
        if isinstance(assertions, Exception):
            entries.append(
                AcceptanceAuditEntry(
                    criterion_id,
                    "fail",
                    source_root=source.root,
                    source_criterion_ids=mapping.assertion_ids,
                    files=mapping.files,
                    note=(
                        "source evidence failed validation: "
                        f"{type(assertions).__name__}: {assertions}"
                    ),
                )
            )
            continue
        failed_assertions = []
        for source_criterion_id in mapping.assertion_ids:
            assertion = assertions.get(source_criterion_id)
            if assertion is None or not assertion.accepted:
                detail = "missing" if assertion is None else assertion.status or "invalid"
                failed_assertions.append(f"{source_criterion_id}={detail}")
        source_root = evidence_root / source.root
        missing_files = [value for value in mapping.files if not (source_root / value).is_file()]
        if failed_assertions or missing_files:
            detail = ", ".join(
                [*failed_assertions, *(f"{value}=missing" for value in missing_files)]
            )
            entries.append(
                AcceptanceAuditEntry(
                    criterion_id,
                    "fail",
                    source_root=source.root,
                    source_criterion_ids=mapping.assertion_ids,
                    files=mapping.files,
                    note=f"source assertion is not an accepted pass: {detail}",
                )
            )
            continue
        entries.append(
            AcceptanceAuditEntry(
                criterion_id,
                "pass",
                source_root=source.root,
                source_criterion_ids=mapping.assertion_ids,
                files=mapping.files,
            )
        )
    return AcceptanceAuditResult(tuple(entries))
