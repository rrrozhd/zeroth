"""Verify a trusted wheel against image files copied out by the Docker daemon."""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import os
import re
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Any

from .models import CandidateIdentity, file_digest

_RUNTIME_MODULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_IMAGE_PYTHON = "/usr/local/bin/python"
_IMAGE_SITE_PACKAGES = "/usr/local/lib/python3.12/site-packages"
RUNTIME_BOOTSTRAP = (
    "import importlib,importlib.util,pathlib,runpy,sys;"
    "site=pathlib.Path(sys.argv[2]).resolve(strict=True);"
    "app=pathlib.Path(sys.argv[3]).resolve(strict=True);"
    "sys.path[:]=[str(site),*sys.path];"
    "zeroth=importlib.import_module('zeroth');"
    "paths=[pathlib.Path(p).resolve(strict=True) for p in zeroth.__path__];"
    "origin=getattr(zeroth,'__file__',None);"
    "ok=(origin is not None and pathlib.Path(origin).resolve(strict=True).is_relative_to("
    "site/'zeroth')) or (origin is None and paths and paths[0]==site/'zeroth');"
    "assert ok,'runtime zeroth import does not originate from verified wheel';"
    "sys.path.insert(0,str(app));"
    "spec=importlib.util.find_spec(sys.argv[4]);"
    "assert spec is not None and spec.origin is not None and pathlib.Path("
    "spec.origin).resolve(strict=True).is_relative_to(app),"
    "'runtime application module does not originate from committed app root';"
    "runpy.run_module(sys.argv[4],run_name='__main__',alter_sys=True)"
)


def _json_object(path: Path, label: str = "wheel installation proof") -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is not readable JSON: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
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


def _runtime_configuration(path: Path, image_digest: str) -> dict[str, str]:
    document = _json_object(path, "image runtime configuration")
    command = document.get("Cmd")
    if document.get("Entrypoint") not in (None, []):
        raise ValueError("certified image runtime must not override its trusted entrypoint")
    if not isinstance(command, list) or any(not isinstance(item, str) for item in command):
        raise ValueError("certified image runtime command must be an executable JSON array")
    if len(command) != 9 or command[:7] != [
        _IMAGE_PYTHON,
        "-I",
        "-S",
        "-c",
        RUNTIME_BOOTSTRAP,
        "run-certified-runtime",
        _IMAGE_SITE_PACKAGES,
    ]:
        raise ValueError("certified image runtime must use the verified-wheel bootstrap")
    app_root, module = command[7:]
    app_path = PurePosixPath(app_root)
    if not app_path.is_absolute() or app_path == PurePosixPath("/") or ".." in app_path.parts:
        raise ValueError("certified image runtime app root must be a contained absolute path")
    if _RUNTIME_MODULE.fullmatch(module) is None:
        raise ValueError("certified image runtime module is invalid")
    if _IMAGE_DIGEST.fullmatch(image_digest) is None:
        raise ValueError("certified image runtime requires an immutable image digest")
    rendered = json.dumps(command, sort_keys=True, separators=(",", ":")).encode()
    return {
        "image_digest": image_digest,
        "command_sha256": "sha256:" + hashlib.sha256(rendered).hexdigest(),
        "site_packages": _IMAGE_SITE_PACKAGES,
        "app_root": app_root,
        "module": module,
    }


def _verify_installed_files(wheel: Path, site_packages: Path) -> tuple[str, str, dict[str, str]]:
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
    return package, version, installed_files


def verify_wheel_installation(
    wheel: Path,
    site_packages: Path,
    output: Path,
    *,
    image_config: Path | None = None,
    image_digest: str | None = None,
) -> None:
    """Write proof that copied image package files exactly match the trusted wheel."""
    if not wheel.is_file() or not site_packages.is_dir():
        raise ValueError("trusted wheel and copied site-packages directory are required")
    package, version, installed_files = _verify_installed_files(wheel, site_packages)
    if (image_config is None) != (image_digest is None):
        raise ValueError("image configuration and digest must be verified together")
    proof: dict[str, Any] = {
        "schema_version": 2 if image_config is not None else 1,
        "package": package,
        "version": version,
        "wheel_sha256": file_digest(wheel),
        "installed_files": dict(sorted(installed_files.items())),
    }
    if image_config is not None and image_digest is not None:
        proof["runtime"] = _runtime_configuration(image_config, image_digest)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    temporary.write_text(json.dumps(proof, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, output)


def validate_wheel_installation(
    proof: Path,
    wheel: Path,
    candidate: CandidateIdentity,
    image_config: Path | None = None,
) -> None:
    """Validate retained installation proof against the exact wheel and candidate version."""
    document = _json_object(proof)
    required = {
        "schema_version",
        "package",
        "version",
        "wheel_sha256",
        "installed_files",
    }
    if image_config is not None:
        required.add("runtime")
    if set(document) != required:
        raise ValueError("wheel installation proof has unexpected fields")
    files = document.get("installed_files")
    if (
        document.get("schema_version") != (2 if image_config is not None else 1)
        or document.get("package") != "zeroth-core"
        or document.get("version") != candidate.zeroth_version
        or document.get("wheel_sha256") != file_digest(wheel)
        or not isinstance(files, dict)
        or not files
    ):
        raise ValueError("wheel installation proof does not match the trusted candidate")
    if image_config is not None:
        expected_runtime = _runtime_configuration(image_config, candidate.image_digest)
        if document.get("runtime") != expected_runtime:
            raise ValueError("wheel installation proof does not match the image runtime")


def build_material_digests(
    candidate: CandidateIdentity,
    sbom: Path,
    certifier_wheel: Path,
    requirements_lock: Path,
    wheel_installation: Path,
    image_config: Path | None,
) -> dict[str, str]:
    """Validate and digest the trusted inputs co-bound to one candidate image."""
    inputs = [
        ("certifier wheel", certifier_wheel),
        ("image requirements lock", requirements_lock),
        ("wheel installation proof", wheel_installation),
    ]
    if image_config is not None:
        inputs.append(("image runtime configuration", image_config))
    for label, path in inputs:
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"{label} is missing or empty")
    validate_wheel_installation(
        wheel_installation, certifier_wheel, candidate, image_config=image_config
    )
    materials = {
        "source": candidate.source_digest,
        "image": candidate.image_digest,
        "sbom": file_digest(sbom),
        "zeroth_wheel": file_digest(certifier_wheel),
        "requirements_lock": file_digest(requirements_lock),
        "wheel_installation": file_digest(wheel_installation),
    }
    if image_config is not None:
        materials["image_config"] = file_digest(image_config)
    return materials
