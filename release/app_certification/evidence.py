"""Content-level identity binding for app certification evidence."""

from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .archives import (
    validate_image_archive as validate_image_archive,
)
from .archives import (
    validate_source_archive as validate_source_archive,
)
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
_COMMAND_OUTPUT_LIMIT = 1 << 20
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


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


def write_provenance(
    path: Path,
    candidate: CandidateIdentity,
    *,
    zeroth_commit: str,
    sbom_digest: str,
    build_material_digests: dict[str, str],
) -> None:
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
            "source_digest": candidate.source_digest,
            "zeroth_version": candidate.zeroth_version,
            "zeroth_commit": zeroth_commit,
            "sbom_digest": sbom_digest,
            "build_material_digests": build_material_digests,
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


def _validated_predicate(statement: dict[str, Any], candidate: CandidateIdentity) -> dict[str, Any]:
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
    predicate = statement.get("predicate")
    if not isinstance(predicate, dict):
        raise ValueError("provenance predicate must be a JSON object")
    expected_identity = {
        "app_commit": candidate.app_commit,
        "source_digest": candidate.source_digest,
        "zeroth_version": candidate.zeroth_version,
        "candidate_identity_digest": identity_digest(candidate),
    }
    required = {
        *expected_identity,
        "zeroth_commit",
        "sbom_digest",
        "build_material_digests",
    }
    if set(predicate) != required or any(
        predicate.get(key) != value for key, value in expected_identity.items()
    ):
        raise ValueError("provenance predicate does not match the exact candidate identity")
    if (
        not isinstance(predicate["zeroth_commit"], str)
        or _COMMIT.fullmatch(predicate["zeroth_commit"]) is None
    ):
        raise ValueError("provenance has an invalid Zeroth commit binding")
    if (
        not isinstance(predicate["sbom_digest"], str)
        or _DIGEST.fullmatch(predicate["sbom_digest"]) is None
    ):
        raise ValueError("provenance has an invalid SBOM digest binding")
    return predicate


def _validate_statement(
    statement: dict[str, Any],
    candidate: CandidateIdentity,
    *,
    zeroth_commit: str | None = None,
    sbom_digest: str | None = None,
    build_material_digests: dict[str, str] | None = None,
) -> None:
    predicate = _validated_predicate(statement, candidate)
    materials = _validated_materials(predicate, candidate)
    if zeroth_commit is not None and predicate["zeroth_commit"] != zeroth_commit:
        raise ValueError("provenance Zeroth commit does not match the trusted certifier")
    if sbom_digest is not None and predicate["sbom_digest"] != sbom_digest:
        raise ValueError("provenance SBOM digest does not match retained bytes")
    if build_material_digests is not None and materials != build_material_digests:
        raise ValueError("provenance build material digests do not match retained inputs")


def _validated_materials(predicate: dict[str, Any], candidate: CandidateIdentity) -> dict[str, str]:
    materials = predicate["build_material_digests"]
    if (
        not isinstance(materials, dict)
        or len(materials) < 4
        or any(
            not isinstance(name, str)
            or not name
            or not isinstance(digest, str)
            or _DIGEST.fullmatch(digest) is None
            for name, digest in materials.items()
        )
    ):
        raise ValueError("provenance build material digests are malformed")
    expected_materials = {
        "source": candidate.source_digest,
        "image": candidate.image_digest,
        "sbom": predicate["sbom_digest"],
    }
    if any(materials.get(name) != value for name, value in expected_materials.items()):
        raise ValueError("provenance build material bindings do not match the candidate")
    return materials


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
    packages = document.get("packages")
    if (
        not isinstance(packages, list)
        or not packages
        or any(not isinstance(item, dict) for item in packages)
    ):
        raise ValueError("SBOM package inventory is missing or malformed")
    if not any(
        item.get("name") == "zeroth-core" and item.get("versionInfo") == candidate.zeroth_version
        for item in packages
    ):
        raise ValueError("SBOM package inventory does not contain the declared zeroth-core")


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
    _validate_statement(
        _statement(_json_object(provenance)),
        report.candidate,
        sbom_digest=file_digest(sbom),
    )


def validate_evidence_subject(
    kind: str,
    path: Path,
    candidate: CandidateIdentity,
    *,
    zeroth_commit: str | None = None,
    sbom_digest: str | None = None,
    build_material_digests: dict[str, str] | None = None,
) -> None:
    """Validate one evidence document against the exact measured candidate."""
    if kind == "sbom":
        _validate_sbom(path, candidate)
        return
    if kind == "provenance":
        _validate_statement(
            _statement(_json_object(path)),
            candidate,
            zeroth_commit=zeroth_commit,
            sbom_digest=sbom_digest,
            build_material_digests=build_material_digests,
        )
        return
    raise ValueError(f"unknown evidence kind {kind!r}")


def _verification_command(
    bundle: Path,
    candidate: CandidateIdentity,
    *,
    repository: str,
    signer_repo: str,
    signer_workflow: str,
    signer_digest: str,
) -> list[str]:
    return [
        "gh",
        "attestation",
        "verify",
        f"oci://{candidate.image_reference}@{candidate.image_digest}",
        "--bundle",
        str(bundle),
        "--predicate-type",
        _PREDICATE_TYPE,
        "--cert-oidc-issuer",
        "https://token.actions.githubusercontent.com",
        "--repo",
        repository,
        "--signer-repo",
        signer_repo,
        "--signer-workflow",
        signer_workflow,
        "--signer-digest",
        signer_digest,
        "--source-digest",
        candidate.app_commit,
        "--deny-self-hosted-runners",
        "--format",
        "json",
    ]


def _verify_attestation(argv: list[str]) -> None:
    try:
        verified = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"attestation signature verification failed: {error}") from error
    if len(verified.stdout) + len(verified.stderr) > _COMMAND_OUTPUT_LIMIT:
        raise ValueError("attestation signature verification output exceeded 1 MiB")
    if verified.returncode:
        detail = verified.stderr.strip() or verified.stdout.strip() or "gh rejected the bundle"
        raise ValueError(f"attestation signature verification failed: {detail[-500:]}")
    try:
        result = json.loads(verified.stdout)
    except json.JSONDecodeError as error:
        raise ValueError("attestation signature verification returned malformed JSON") from error
    if not isinstance(result, (dict, list)):
        raise ValueError("attestation signature verification returned malformed JSON")


def verify_finalized_attestation(
    report_path: Path,
    root: Path,
    *,
    repository: str | None,
    signer_repo: str | None,
    signer_workflow: str | None,
    signer_digest: str | None,
) -> CertificationReport:
    """Reverify the finalized signed provenance bound into a passing report."""
    report = validate_report(report_path, root=root)
    if report.candidate is None or report.evidence is None:
        raise ValueError("finalized attestation requires a passing report")
    provenance = _resolve(root, report.evidence.provenance.path)
    document = _json_object(provenance)
    if not isinstance(document.get("dsseEnvelope"), dict):
        raise ValueError("promotion requires finalized signed attestation evidence")
    if (
        not isinstance(repository, str)
        or not repository.strip()
        or not isinstance(signer_repo, str)
        or not signer_repo.strip()
        or not isinstance(signer_workflow, str)
        or not signer_workflow.strip()
        or not isinstance(signer_digest, str)
        or not signer_digest.strip()
    ):
        raise ValueError("promotion requires a complete attestation trust policy")
    subject_repo = repository.strip().casefold()
    signer_is_subject = signer_repo.strip().casefold() == subject_repo
    workflow_is_subject = signer_workflow.strip().casefold().startswith(f"{subject_repo}/")
    if signer_is_subject or workflow_is_subject:
        raise ValueError("promotion rejects self-authored attestation evidence")
    _validate_statement(_statement(document), report.candidate)
    _verify_attestation(
        _verification_command(
            provenance,
            report.candidate,
            repository=repository,
            signer_repo=signer_repo,
            signer_workflow=signer_workflow,
            signer_digest=signer_digest,
        )
    )
    return report


def finalize_attestation(
    bundle: Path,
    report_path: Path,
    root: Path,
    *,
    repository: str,
    signer_repo: str,
    signer_workflow: str,
    signer_digest: str,
) -> CertificationReport:
    """Replace the unsigned predicate with its signed bundle and rebind the report."""
    report = validate_report(report_path, root=root)
    if report.candidate is None or report.evidence is None:
        raise ValueError("attestation finalization requires a passing report")
    document = _json_object(bundle)
    _validate_statement(_statement(document), report.candidate)
    _verify_attestation(
        _verification_command(
            bundle,
            report.candidate,
            repository=repository,
            signer_repo=signer_repo,
            signer_workflow=signer_workflow,
            signer_digest=signer_digest,
        )
    )
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
