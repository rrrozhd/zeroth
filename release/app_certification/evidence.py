"""Content-level identity binding for app certification evidence."""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import tarfile
from pathlib import Path, PurePosixPath
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
_JSON_LIMIT = 16 * 1024 * 1024
_CONFIG_LIMIT = 4 * 1024 * 1024


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


def _archive_members(archive: tarfile.TarFile) -> dict[str, tarfile.TarInfo]:
    members: dict[str, tarfile.TarInfo] = {}
    for member in archive.getmembers():
        name = member.name
        path = PurePosixPath(name)
        if not name or "\\" in name or path.is_absolute() or ".." in path.parts:
            raise ValueError(f"image archive has unsafe archive member {name!r}")
        if not (member.isfile() or member.isdir()):
            raise ValueError(f"image archive has unsafe archive member type for {name!r}")
        if name in members:
            raise ValueError(f"image archive has duplicate member {name!r}")
        members[name] = member
    return members


def _member_bytes(
    archive: tarfile.TarFile,
    members: dict[str, tarfile.TarInfo],
    name: str,
    limit: int,
) -> bytes:
    member = members.get(name)
    if member is None or not member.isfile():
        raise ValueError(f"image archive member {name!r} is missing")
    if member.size > limit:
        raise ValueError(f"image archive member {name!r} exceeds its validation limit")
    stream = archive.extractfile(member)
    if stream is None:
        raise ValueError(f"image archive member {name!r} is unreadable")
    return stream.read()


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _member_sha256(
    archive: tarfile.TarFile,
    members: dict[str, tarfile.TarInfo],
    name: str,
) -> str:
    member = members.get(name)
    if member is None or not member.isfile():
        raise ValueError(f"image archive member {name!r} is missing")
    stream = archive.extractfile(member)
    if stream is None:
        raise ValueError(f"image archive member {name!r} is unreadable")
    hasher = hashlib.sha256()
    while chunk := stream.read(1024 * 1024):
        hasher.update(chunk)
    return "sha256:" + hasher.hexdigest()


def _descriptor_digest(descriptor: Any) -> str:
    if not isinstance(descriptor, dict):
        raise ValueError("image archive descriptor must be a JSON object")
    digest = descriptor.get("digest")
    if (
        not isinstance(digest, str)
        or not digest.startswith("sha256:")
        or len(digest) != 71
        or any(char not in "0123456789abcdef" for char in digest[7:])
    ):
        raise ValueError("image archive descriptor has an invalid sha256 digest")
    return digest


def _validate_blob(
    archive: tarfile.TarFile,
    members: dict[str, tarfile.TarInfo],
    descriptor: Any,
    limit: int | None,
) -> bytes | None:
    digest = _descriptor_digest(descriptor)
    name = f"blobs/sha256/{digest[7:]}"
    member = members.get(name)
    if member is None or not member.isfile():
        raise ValueError(f"image archive member {name!r} is missing")
    if descriptor.get("size") != member.size:
        raise ValueError("image archive descriptor size does not match its blob")
    if limit is not None and member.size > limit:
        raise ValueError(f"image archive member {name!r} exceeds its validation limit")
    stream = archive.extractfile(member)
    if stream is None:
        raise ValueError(f"image archive member {name!r} is unreadable")
    hasher = hashlib.sha256()
    chunks: list[bytes] | None = [] if limit is not None else None
    while chunk := stream.read(1024 * 1024):
        hasher.update(chunk)
        if chunks is not None:
            chunks.append(chunk)
    if "sha256:" + hasher.hexdigest() != digest:
        raise ValueError("image archive descriptor digest does not match its blob")
    return b"".join(chunks) if chunks is not None else None


def _validate_descriptor(
    archive: tarfile.TarFile,
    members: dict[str, tarfile.TarInfo],
    descriptor: Any,
    seen: set[str],
) -> None:
    digest = _descriptor_digest(descriptor)
    if digest in seen:
        raise ValueError("image archive descriptor graph contains a cycle")
    data = _validate_blob(archive, members, descriptor, _JSON_LIMIT)
    assert data is not None
    try:
        document = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("image archive descriptor blob is not JSON") from error
    if not isinstance(document, dict):
        raise ValueError("image archive descriptor blob must contain a JSON object")
    seen.add(digest)
    if isinstance(document.get("manifests"), list) and document["manifests"]:
        for child in document["manifests"]:
            _validate_descriptor(archive, members, child, seen)
    elif "config" in document:
        _validate_blob(archive, members, document["config"], _CONFIG_LIMIT)
        layers = document.get("layers", [])
        if not isinstance(layers, list):
            raise ValueError("image archive manifest layers must be a list")
        for layer in layers:
            _validate_blob(archive, members, layer, None)
    else:
        raise ValueError("image archive descriptor is neither an index nor an image manifest")
    seen.remove(digest)


def _validate_oci_archive(
    archive: tarfile.TarFile,
    members: dict[str, tarfile.TarInfo],
    candidate: CandidateIdentity,
) -> None:
    try:
        index = json.loads(_member_bytes(archive, members, "index.json", _JSON_LIMIT))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("image archive index.json is not readable JSON") from error
    manifests = index.get("manifests") if isinstance(index, dict) else None
    if not isinstance(manifests, list) or len(manifests) != 1:
        raise ValueError("image archive index must name exactly one candidate descriptor")
    if _descriptor_digest(manifests[0]) != candidate.image_digest:
        raise ValueError("image archive descriptor digest does not match the candidate")
    _validate_descriptor(archive, members, manifests[0], set())


def _classic_diff_ids(config: bytes) -> list[Any]:
    try:
        document = json.loads(config)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("image archive config is not readable JSON") from error
    rootfs = document.get("rootfs") if isinstance(document, dict) else None
    diff_ids = rootfs.get("diff_ids") if isinstance(rootfs, dict) else None
    if not isinstance(diff_ids, list):
        raise ValueError("image archive config must bind its saved layers")
    return diff_ids


def _validate_classic_archive(
    archive: tarfile.TarFile,
    members: dict[str, tarfile.TarInfo],
    candidate: CandidateIdentity,
) -> None:
    try:
        manifest = json.loads(_member_bytes(archive, members, "manifest.json", 1024 * 1024))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("image archive manifest.json is not readable JSON") from error
    if not isinstance(manifest, list) or len(manifest) != 1 or not isinstance(manifest[0], dict):
        raise ValueError("image archive must contain exactly one image")
    config_name = manifest[0].get("Config")
    if not isinstance(config_name, str):
        raise ValueError("image archive config path is missing")
    config = _member_bytes(archive, members, config_name, _CONFIG_LIMIT)
    if _sha256(config) != candidate.image_digest:
        raise ValueError("image archive config digest does not match the candidate")
    diff_ids = _classic_diff_ids(config)
    layers = manifest[0].get("Layers")
    if not isinstance(layers, list):
        raise ValueError("image archive config must bind its saved layers")
    if len(diff_ids) != len(layers):
        raise ValueError("image archive layer count does not match config rootfs")
    for layer_name, diff_id in zip(layers, diff_ids, strict=True):
        if not isinstance(layer_name, str):
            raise ValueError("image archive layer path is invalid")
        expected = _descriptor_digest({"digest": diff_id})
        if _member_sha256(archive, members, layer_name) != expected:
            raise ValueError("image archive layer digest does not match config rootfs")


def validate_image_archive(path: Path, candidate: CandidateIdentity) -> None:
    """Bind a classic Docker config or OCI root descriptor to the candidate digest."""
    try:
        with tarfile.open(path, mode="r:") as archive:
            members = _archive_members(archive)
            if "index.json" in members:
                _validate_oci_archive(archive, members, candidate)
            else:
                _validate_classic_archive(archive, members, candidate)
    except (OSError, tarfile.TarError) as error:
        raise ValueError(f"image archive is unreadable: {error}") from error
