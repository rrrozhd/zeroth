#!/usr/bin/env python3
"""Write one atomic receipt from the exact source and raw load observations."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from release.load.environment import (  # noqa: E402
    observation_digest,
    runtime_environment,
    runtime_service_instances,
)

REVISION = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
SOURCE_IDENTITY_KEYS = {
    "schema_version",
    "commit",
    "tree",
    "package_version",
    "source_digest",
}


def source_digest(root: Path) -> str:
    """Hash extracted Git source in canonical relative POSIX path order."""
    digest = hashlib.sha256()
    files = (path for path in root.rglob("*") if path.is_file())
    for path in sorted(files, key=lambda path: path.relative_to(root).as_posix()):
        relative = path.relative_to(root)
        if relative.parts[0] == ".git":
            continue
        digest.update(relative.as_posix().encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return "sha256:" + digest.hexdigest()


def load_source_identity(path: Path) -> tuple[dict, str]:
    """Load the retained exact-source identity and bind its bytes."""
    raw = path.read_bytes()
    identity = json.loads(raw)
    valid = (
        isinstance(identity, dict)
        and set(identity) == SOURCE_IDENTITY_KEYS
        and identity.get("schema_version") == 1
        and REVISION.fullmatch(str(identity.get("commit", ""))) is not None
        and REVISION.fullmatch(str(identity.get("tree", ""))) is not None
        and DIGEST.fullmatch(str(identity.get("source_digest", ""))) is not None
        and isinstance(identity.get("package_version"), str)
        and bool(identity["package_version"].strip())
    )
    if not valid:
        raise ValueError("baseline source identity is malformed")
    return identity, "sha256:" + hashlib.sha256(raw).hexdigest()


def source_identity(source: Path, identity_path: Path) -> dict[str, str]:
    """Verify the mounted source against its retained Git-derived identity."""
    identity, identity_digest = load_source_identity(identity_path)
    measured_digest = source_digest(source)
    package = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))
    if measured_digest != identity["source_digest"]:
        raise ValueError("receipt source does not match the exact-base identity")
    if package["project"]["version"] != identity["package_version"]:
        raise ValueError("receipt package version does not match the exact-base identity")
    return {
        "commit": identity["commit"],
        "tree": identity["tree"],
        "source_digest": measured_digest,
        "source_identity_digest": identity_digest,
    }


def build_receipt(source: Path, raw: Path, identity_path: Path) -> dict:
    """Derive the receipt inside the same environment that executed the probe."""
    identity = source_identity(source, identity_path)
    if any(REVISION.fullmatch(identity[name]) is None for name in ("commit", "tree")):
        raise ValueError("measured receipt commit/tree is malformed")
    observations = json.loads(raw.read_text(encoding="utf-8"))
    if not isinstance(observations, list) or not observations:
        raise ValueError("receipt observations are empty")
    from zeroth.service import app

    origin = Path(app.__file__).resolve()
    origin.relative_to(source.resolve())
    package = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))
    environment = runtime_environment()
    return {
        **identity,
        "package_version": package["project"]["version"],
        "product_import_origin": str(origin),
        "observation_digest": observation_digest(observations),
        "environment": environment,
        "service_instances": runtime_service_instances(environment),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-identity", type=Path, required=True)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    receipt = build_receipt(args.source, args.raw, args.source_identity)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
