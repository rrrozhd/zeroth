"""Content validation for retained source and image archives."""

from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any

from .models import CandidateIdentity, file_digest

_JSON_LIMIT = 16 * 1024 * 1024
_CONFIG_LIMIT = 4 * 1024 * 1024
_ARCHIVE_LIMIT = 8 * 1024 * 1024 * 1024
_ARCHIVE_MEMBER_LIMIT = 100_000


def _archive_members(archive: tarfile.TarFile) -> dict[str, tarfile.TarInfo]:
    members: dict[str, tarfile.TarInfo] = {}
    total_size = 0
    for member in archive:
        if len(members) >= _ARCHIVE_MEMBER_LIMIT:
            raise ValueError("image archive exceeds the member-count validation limit")
        name = member.name
        path = PurePosixPath(name)
        if not name or "\\" in name or path.is_absolute() or ".." in path.parts:
            raise ValueError(f"image archive has unsafe archive member {name!r}")
        if not (member.isfile() or member.isdir()):
            raise ValueError(f"image archive has unsafe archive member type for {name!r}")
        if name in members:
            raise ValueError(f"image archive has duplicate member {name!r}")
        total_size += member.size
        if total_size > _ARCHIVE_LIMIT:
            raise ValueError("image archive exceeds the 8 GiB content validation limit")
        members[name] = member
    return members


def _image_names(reference: str) -> set[str]:
    names = {reference}
    first = reference.partition("/")[0]
    if "/" not in reference:
        names.add(f"docker.io/library/{reference}")
    elif "." not in first and ":" not in first and first != "localhost":
        names.add(f"docker.io/{reference}")
    return names


def _require_image_name(raw: Any, candidate: CandidateIdentity) -> None:
    names = raw if isinstance(raw, list) else [raw]
    if not names or any(not isinstance(name, str) for name in names):
        raise ValueError("image archive name binding is missing or malformed")
    if not _image_names(candidate.image_reference).intersection(names):
        raise ValueError("image archive name does not match the candidate reference")


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
    descriptor = manifests[0]
    if _descriptor_digest(descriptor) != candidate.image_digest:
        raise ValueError("image archive descriptor digest does not match the candidate")
    annotations = descriptor.get("annotations") if isinstance(descriptor, dict) else None
    if not isinstance(annotations, dict):
        raise ValueError("image archive candidate descriptor has no name annotation")
    name = annotations.get("io.containerd.image.name")
    if name is None:
        name = annotations.get("org.opencontainers.image.ref.name")
    _require_image_name(name, candidate)
    _validate_descriptor(archive, members, descriptor, set())


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
    _require_image_name(manifest[0].get("RepoTags"), candidate)
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
        if path.stat().st_size > _ARCHIVE_LIMIT:
            raise ValueError("image archive exceeds the 8 GiB validation limit")
        with tarfile.open(path, mode="r:") as archive:
            members = _archive_members(archive)
            if "index.json" in members:
                _validate_oci_archive(archive, members, candidate)
            else:
                _validate_classic_archive(archive, members, candidate)
    except (OSError, tarfile.TarError) as error:
        raise ValueError(f"image archive is unreadable: {error}") from error


def validate_source_archive(path: Path, candidate: CandidateIdentity) -> None:
    """Bind the exact Git archive used as the Docker source context."""
    try:
        if file_digest(path) != candidate.source_digest:
            raise ValueError("source archive digest does not match the candidate")
        with tarfile.open(path, mode="r:") as archive:
            archived_commit = archive.pax_headers.get("comment")
    except (OSError, tarfile.TarError) as error:
        raise ValueError(f"source archive is unreadable: {error}") from error
    if archived_commit != candidate.app_commit:
        raise ValueError("source archive commit does not match the candidate")
