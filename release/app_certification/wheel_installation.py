"""Verify a trusted wheel against image files copied out by the Docker daemon."""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import os
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Any

from .models import CandidateIdentity, file_digest


def _json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"wheel installation proof is not readable JSON: {error}") from error
    if not isinstance(value, dict):
        raise ValueError("wheel installation proof must be a JSON object")
    return value


def _installed_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"wheel member {name!r} escapes site-packages")
    if len(path.parts) >= 3 and path.parts[0].endswith(".data"):
        if path.parts[1] not in {"purelib", "platlib"}:
            raise ValueError(f"wheel member {name!r} has unsupported install scheme")
        path = PurePosixPath(*path.parts[2:])
    return path


def _record_rows(archive: zipfile.ZipFile) -> tuple[str, list[list[str]]]:
    records = [name for name in archive.namelist() if name.endswith(".dist-info/RECORD")]
    if len(records) != 1:
        raise ValueError("trusted wheel must contain exactly one dist-info RECORD")
    try:
        rows = list(csv.reader(archive.read(records[0]).decode("utf-8").splitlines()))
    except (KeyError, UnicodeDecodeError, csv.Error) as error:
        raise ValueError("trusted wheel RECORD is malformed") from error
    if any(len(row) != 3 for row in rows):
        raise ValueError("trusted wheel RECORD rows must have three fields")
    return records[0], rows


def _expected_digest(encoded: str) -> bytes:
    algorithm, separator, value = encoded.partition("=")
    if separator != "=" or algorithm != "sha256" or not value:
        raise ValueError("trusted wheel RECORD must use sha256 for every installed file")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except ValueError as error:
        raise ValueError("trusted wheel RECORD contains an invalid sha256 digest") from error


def _metadata(archive: zipfile.ZipFile) -> tuple[str, str]:
    names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
    if len(names) != 1:
        raise ValueError("trusted wheel must contain exactly one dist-info METADATA")
    message = BytesParser().parsebytes(archive.read(names[0]))
    package = str(message.get("Name", "")).lower().replace("_", "-")
    version = str(message.get("Version", ""))
    if package != "zeroth-core" or not version:
        raise ValueError("trusted wheel metadata must identify versioned zeroth-core")
    return package, version


def _reject_extra_package_files(site_packages: Path, expected: set[PurePosixPath]) -> None:
    top_levels = {
        path.parts[0]
        for path in expected
        if path.parts and not path.parts[0].endswith((".dist-info", ".data"))
    }
    if not top_levels:
        raise ValueError("trusted wheel contains no importable package files")
    for top_level in top_levels:
        package_root = site_packages / top_level
        actual = {
            PurePosixPath(path.relative_to(site_packages).as_posix())
            for path in package_root.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
        }
        wanted = {path for path in expected if path.parts and path.parts[0] == top_level}
        if actual != wanted:
            raise ValueError(f"installed {top_level} package files do not exactly match the wheel")
        shadows = [
            path
            for path in site_packages.glob(f"{top_level}.*")
            if path.is_file() and path.suffix != ".pyc"
        ]
        if shadows:
            raise ValueError(f"installed {top_level} package has a shadowing top-level module")


def verify_wheel_installation(wheel: Path, site_packages: Path, output: Path) -> None:
    """Write proof that copied image package files exactly match the trusted wheel."""
    if not wheel.is_file() or not site_packages.is_dir():
        raise ValueError("trusted wheel and copied site-packages directory are required")
    installed_files: dict[str, str] = {}
    expected_paths: set[PurePosixPath] = set()
    try:
        with zipfile.ZipFile(wheel) as archive:
            record_name, rows = _record_rows(archive)
            package, version = _metadata(archive)
            for name, encoded_digest, encoded_size in rows:
                if name == record_name:
                    continue
                member = archive.read(name)
                expected = _expected_digest(encoded_digest)
                if hashlib.sha256(member).digest() != expected or len(member) != int(encoded_size):
                    raise ValueError(f"trusted wheel member {name!r} does not match RECORD")
                relative = _installed_path(name)
                installed = site_packages.joinpath(*relative.parts)
                if (
                    installed.is_symlink()
                    or not installed.is_file()
                    or installed.read_bytes() != member
                ):
                    raise ValueError(
                        f"installed wheel member {relative.as_posix()!r} does not match"
                    )
                expected_paths.add(relative)
                installed_files[relative.as_posix()] = (
                    "sha256:" + hashlib.sha256(member).hexdigest()
                )
            _reject_extra_package_files(site_packages, expected_paths)
    except (KeyError, OSError, ValueError, zipfile.BadZipFile) as error:
        if isinstance(error, ValueError):
            raise
        raise ValueError(f"trusted wheel is malformed: {error}") from error
    proof = {
        "schema_version": 1,
        "package": package,
        "version": version,
        "wheel_sha256": file_digest(wheel),
        "installed_files": dict(sorted(installed_files.items())),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    temporary.write_text(json.dumps(proof, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, output)


def validate_wheel_installation(
    proof: Path, wheel: Path, candidate: CandidateIdentity
) -> None:
    """Validate retained installation proof against the exact wheel and candidate version."""
    document = _json_object(proof)
    if set(document) != {
        "schema_version",
        "package",
        "version",
        "wheel_sha256",
        "installed_files",
    }:
        raise ValueError("wheel installation proof has unexpected fields")
    files = document.get("installed_files")
    if (
        document.get("schema_version") != 1
        or document.get("package") != "zeroth-core"
        or document.get("version") != candidate.zeroth_version
        or document.get("wheel_sha256") != file_digest(wheel)
        or not isinstance(files, dict)
        or not files
    ):
        raise ValueError("wheel installation proof does not match the trusted candidate")
