"""Explicit raw-to-approved TapeV1 curation transaction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from zeroth.check.tape.models import RawRecordingV1, TapeV1, ToolOccurrenceV1
from zeroth.check.tape.normalization import action_identity_v1, argument_fingerprint
from zeroth.check.tape.scrubbing import SecretFinding, SecretScanner, scrub_secrets
from zeroth.check.tape.storage import TapeStorageError, atomic_write


class CurationError(RuntimeError):
    """Curation cannot safely produce an approved tape."""


@dataclass(frozen=True, slots=True)
class CurationManifest:
    finding_count: int
    finding_paths: tuple[str, ...]
    identity_changed_by_scrubbing: bool


@dataclass(frozen=True, slots=True)
class CurationResult:
    tape: TapeV1
    manifest: CurationManifest


def _scrub_occurrence(
    raw: RawRecordingV1,
    occurrence: ToolOccurrenceV1,
    *,
    allowlist: set[str] | None,
) -> tuple[ToolOccurrenceV1, tuple[SecretFinding, ...], bool]:
    scrubbed_arguments = scrub_secrets(dict(occurrence.arguments), allowlist=allowlist)
    scrubbed_result = scrub_secrets(occurrence.result, allowlist=allowlist)
    arguments = scrubbed_arguments.value
    fingerprint = argument_fingerprint(arguments)
    identity = action_identity_v1(
        case_id=raw.case_id,
        scenario_run_id=raw.scenario_run_id,
        tool_name=occurrence.name,
        input_schema_digest=occurrence.input_schema_digest,
        tool_call_id=occurrence.tool_call_id,
        argument_fingerprint=fingerprint,
    )
    data = occurrence.model_dump(mode="json")
    data.update(
        arguments=arguments,
        argument_fingerprint=fingerprint,
        result=scrubbed_result.value,
        action_identity=identity,
    )
    return (
        ToolOccurrenceV1.model_validate(data),
        scrubbed_arguments.findings + scrubbed_result.findings,
        identity != occurrence.action_identity,
    )


def curate_raw_recording(
    raw_path: str | Path,
    *,
    output: str | Path,
    reviewer_id: str,
    approved_at: str | None = None,
    allowlist: set[str] | None = None,
    overwrite: bool = False,
) -> CurationResult:
    if not isinstance(reviewer_id, str) or not reviewer_id.strip():
        raise CurationError("reviewer identity is required")
    try:
        raw = RawRecordingV1.model_validate_json(Path(raw_path).read_bytes())
    except (OSError, ValidationError) as exc:
        raise CurationError("invalid raw recording") from exc

    content_findings: list[SecretFinding] = []
    case_input = scrub_secrets(raw.case_input, allowlist=allowlist)
    invocation_config = scrub_secrets(dict(raw.invocation_config), allowlist=allowlist)
    content_findings.extend(case_input.findings)
    content_findings.extend(invocation_config.findings)
    occurrences: list[ToolOccurrenceV1] = []
    identity_changed = False
    for occurrence in raw.tool_occurrences:
        curated, findings, changed = _scrub_occurrence(raw, occurrence, allowlist=allowlist)
        occurrences.append(curated)
        content_findings.extend(findings)
        identity_changed = identity_changed or changed

    common: dict[str, Any] = raw.model_dump(
        mode="json", exclude={"schema_version", "source_digest"}
    )
    common.update(
        case_input=case_input.value,
        invocation_config=invocation_config.value,
        tool_occurrences=[item.model_dump(mode="json") for item in occurrences],
    )
    scanner = SecretScanner(allowlist=allowlist)
    if scanner.scan(
        {
            "case_input": common["case_input"],
            "invocation_config": common["invocation_config"],
            "tool_content": [
                {"arguments": item.arguments, "result": item.result} for item in occurrences
            ],
        }
    ):
        raise CurationError("blocking secret findings remain after scrubbing")

    data = {
        "schema_version": "tape.v1",
        **common,
        "raw_source_digest": raw.source_digest,
        "scrubber_version": "scrubber.v1",
        "secret_rules_version": "secret_rules.v1",
        "reviewer_id": reviewer_id.strip(),
        "approved_at": approved_at or datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "identity_changed_by_scrubbing": identity_changed,
    }
    from zeroth.check.tape.normalization import sha256_digest

    data["curated_content_digest"] = sha256_digest(data)
    try:
        tape = TapeV1.model_validate(data)
        atomic_write(Path(output), tape.canonical_bytes(), overwrite=overwrite)
        reloaded = TapeV1.model_validate_json(Path(output).read_bytes())
    except (ValidationError, TapeStorageError, OSError) as exc:
        raise CurationError(str(exc)) from exc
    manifest = CurationManifest(
        finding_count=len(content_findings),
        finding_paths=tuple(sorted({finding.path for finding in content_findings})),
        identity_changed_by_scrubbing=identity_changed,
    )
    return CurationResult(tape=reloaded, manifest=manifest)
