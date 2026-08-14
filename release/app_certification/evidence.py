"""Content-level identity binding for app certification evidence."""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import tarfile
from pathlib import Path
from typing import Any

from .models import (
    CandidateIdentity,
    CertificationReport,
    EvidenceFile,
    file_digest,
    identity_digest,
    validate_report,
    write_report,
)

_ANNOTATOR = "Tool: Zeroth app-certification"
_COMMENT_PREFIX = "zeroth-candidate:"
_PREDICATE_TYPE = "https://zeroth.dev/app-certification/provenance/v1"


def _json_object(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError(f"evidence {path} exceeds the 16 MiB validation limit")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"evidence {path} is not readable JSON: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"evidence {path} must contain a JSON object")
    return value


def _resolve(root: Path, relative: str) -> Path:
    root = root.resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError(f"evidence path {relative!r} escapes the validation root") from error
    return path


def bind_sbom(path: Path, candidate: CandidateIdentity) -> None:
    """Add one deterministic SPDX annotation for the measured candidate."""
    document = _json_object(path)
    if not str(document.get("spdxVersion", "")).startswith("SPDX-"):
        raise ValueError("SBOM must be an SPDX JSON document")
    annotations = document.setdefault("annotations", [])
    if not isinstance(annotations, list):
        raise ValueError("SBOM annotations must be a list")
    if any(not isinstance(item, dict) for item in annotations):
        raise ValueError("SBOM annotations must contain JSON objects")
    annotations[:] = [item for item in annotations if item.get("annotator") != _ANNOTATOR]
    subject = json.dumps(candidate.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    annotations.append(
        {
            "annotationDate": "1970-01-01T00:00:00Z",
            "annotationType": "OTHER",
            "annotator": _ANNOTATOR,
            "comment": _COMMENT_PREFIX + subject,
        }
    )
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_provenance(path: Path, candidate: CandidateIdentity) -> None:
    """Write the exact custom predicate later signed by the privileged job."""
    statement = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [
            {
                "name": candidate.image_reference,
                "digest": {"sha256": candidate.image_digest.removeprefix("sha256:")},
            }
        ],
        "predicateType": _PREDICATE_TYPE,
        "predicate": {
            "app_commit": candidate.app_commit,
            "zeroth_version": candidate.zeroth_version,
            "candidate_identity_digest": identity_digest(candidate),
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(statement, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _statement(document: dict[str, Any]) -> dict[str, Any]:
    if document.get("_type") == "https://in-toto.io/Statement/v1":
        return document
    envelope = document.get("dsseEnvelope")
    if not isinstance(envelope, dict) or not isinstance(envelope.get("payload"), str):
        raise ValueError("provenance must be an in-toto statement or Sigstore bundle")
    try:
        payload = base64.b64decode(envelope["payload"], validate=True)
        statement = json.loads(payload)
    except (ValueError, json.JSONDecodeError) as error:
        raise ValueError("provenance bundle contains an invalid DSSE payload") from error
    if not isinstance(statement, dict):
        raise ValueError("provenance DSSE payload must contain a JSON object")
    return statement


def _validate_statement(statement: dict[str, Any], candidate: CandidateIdentity) -> None:
    expected_subject = [
        {
            "name": candidate.image_reference,
            "digest": {"sha256": candidate.image_digest.removeprefix("sha256:")},
        }
    ]
    if statement.get("subject") != expected_subject:
        raise ValueError("provenance subject does not match the candidate image")
    if statement.get("predicateType") != _PREDICATE_TYPE:
        raise ValueError("provenance predicate type is not the app-certification contract")
    expected = {
        "app_commit": candidate.app_commit,
        "zeroth_version": candidate.zeroth_version,
        "candidate_identity_digest": identity_digest(candidate),
    }
    if statement.get("predicate") != expected:
        raise ValueError("provenance predicate does not match the exact candidate identity")


def _validate_sbom(path: Path, candidate: CandidateIdentity) -> None:
    document = _json_object(path)
    if not str(document.get("spdxVersion", "")).startswith("SPDX-"):
        raise ValueError("SBOM must be an SPDX JSON document")
    comments = [
        item.get("comment", "")
        for item in document.get("annotations", [])
        if isinstance(item, dict) and item.get("annotator") == _ANNOTATOR
    ]
    expected = _COMMENT_PREFIX + json.dumps(
        candidate.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    if comments != [expected]:
        raise ValueError("SBOM subject does not match the exact candidate identity")


def validate_evidence(report: CertificationReport, root: Path) -> None:
    """Recompute retained bytes and validate both embedded subjects."""
    if report.candidate is None or report.evidence is None:
        raise ValueError("passing report is missing candidate-bound evidence")
    sbom = _resolve(root, report.evidence.sbom.path)
    provenance = _resolve(root, report.evidence.provenance.path)
    for label, path, record in (
        ("SBOM", sbom, report.evidence.sbom),
        ("provenance", provenance, report.evidence.provenance),
    ):
        if not path.is_file() or file_digest(path) != record.sha256:
            raise ValueError(f"{label} sha256 does not match retained bytes")
    _validate_sbom(sbom, report.candidate)
    _validate_statement(_statement(_json_object(provenance)), report.candidate)


def validate_evidence_subject(kind: str, path: Path, candidate: CandidateIdentity) -> None:
    """Validate one evidence document against the exact measured candidate."""
    if kind == "sbom":
        _validate_sbom(path, candidate)
        return
    if kind == "provenance":
        _validate_statement(_statement(_json_object(path)), candidate)
        return
    raise ValueError(f"unknown evidence kind {kind!r}")


def finalize_attestation(bundle: Path, report_path: Path, root: Path) -> CertificationReport:
    """Replace the unsigned predicate with its signed bundle and rebind the report."""
    report = validate_report(report_path, root=root)
    if report.candidate is None or report.evidence is None:
        raise ValueError("attestation finalization requires a passing report")
    document = _json_object(bundle)
    _validate_statement(_statement(document), report.candidate)
    destination = _resolve(root, report.evidence.provenance.path)
    shutil.copyfile(bundle, destination)
    evidence = report.evidence.model_copy(
        update={
            "provenance": EvidenceFile(
                path=report.evidence.provenance.path,
                sha256=file_digest(destination),
            )
        }
    )
    final = report.model_copy(update={"evidence": evidence})
    write_report(final, report_path)
    return validate_report(report_path, root=root)


def validate_image_archive(path: Path, candidate: CandidateIdentity) -> None:
    """Prove a Docker save archive contains the config named by the image digest."""
    try:
        with tarfile.open(path, mode="r:") as archive:
            manifest_member = archive.getmember("manifest.json")
            if manifest_member.size > 1024 * 1024:
                raise ValueError("image archive manifest exceeds 1 MiB")
            manifest_file = archive.extractfile(manifest_member)
            if manifest_file is None:
                raise ValueError("image archive has no manifest.json")
            manifest = json.load(manifest_file)
            if (
                not isinstance(manifest, list)
                or len(manifest) != 1
                or not isinstance(manifest[0], dict)
            ):
                raise ValueError("image archive must contain exactly one image")
            config_name = manifest[0].get("Config")
            if not isinstance(config_name, str) or Path(config_name).name != config_name:
                raise ValueError("image archive has an unsafe config path")
            config_member = archive.getmember(config_name)
            if config_member.size > 4 * 1024 * 1024:
                raise ValueError("image archive config exceeds 4 MiB")
            config_file = archive.extractfile(config_member)
            if config_file is None:
                raise ValueError("image archive config is missing")
            digest = "sha256:" + hashlib.sha256(config_file.read()).hexdigest()
    except (OSError, KeyError, tarfile.TarError, json.JSONDecodeError) as error:
        raise ValueError(f"image archive is unreadable: {error}") from error
    if digest != candidate.image_digest:
        raise ValueError("image archive config digest does not match the candidate")
