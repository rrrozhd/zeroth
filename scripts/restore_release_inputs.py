#!/usr/bin/env python3
"""Restore pinned release reference inputs from an external archive."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _destination(root: Path, name: str) -> Path:
    parts = PurePosixPath(name)
    if (
        parts.is_absolute()
        or ".." in parts.parts
        or "\\" in name
        or parts.as_posix() != name
        or not parts.parts
    ):
        raise ValueError(f"invalid input path: {name}")
    target = root
    for part in parts.parts:
        target /= part
        if target.is_symlink():
            raise ValueError(f"destination contains a symlink: {name}")
    tracked = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--error-unmatch", "--", name],
        capture_output=True,
    )
    if tracked.returncode == 0:
        raise ValueError(f"input is tracked: {name}")
    if tracked.returncode != 1:
        raise ValueError("cannot inspect the repository index")
    ignored = subprocess.run(
        ["git", "-C", str(root), "check-ignore", "--quiet", "--", name],
        capture_output=True,
    )
    if ignored.returncode != 0:
        raise ValueError(f"input is not ignored: {name}")
    return target


def restore(root: Path, archive: Path, manifest: Path) -> int:
    """Validate the complete bundle before creating any missing input files."""
    expected = json.loads(manifest.read_text(encoding="utf-8"))
    if expected.get("schema_version") != 1 or not expected.get("files"):
        raise ValueError("unsupported or empty input manifest")
    if archive.stat().st_size != expected["archive_size"]:
        raise ValueError("archive size mismatch")
    raw = archive.read_bytes()
    if _digest(raw) != expected["archive_sha256"]:
        raise ValueError("archive digest mismatch")
    contents: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as bundle:
        for member in bundle:
            spec = expected["files"].get(member.name)
            if (
                spec is None
                or member.name in contents
                or not member.isfile()
                or member.sparse
                or member.size != spec["size"]
            ):
                raise ValueError(f"invalid archive member: {member.name}")
            stream = bundle.extractfile(member)
            if stream is None:
                raise ValueError(f"invalid archive member: {member.name}")
            data = stream.read(spec["size"] + 1)
            if len(data) != spec["size"] or _digest(data) != spec["sha256"]:
                raise ValueError(f"file digest mismatch: {member.name}")
            contents[member.name] = data
    if contents.keys() != expected["files"].keys():
        raise ValueError("archive is missing required inputs")
    destinations = {name: _destination(root, name) for name in contents}
    for name, target in destinations.items():
        if target.exists() and (not target.is_file() or target.read_bytes() != contents[name]):
            raise ValueError(f"existing input differs: {name}")
    for name, target in destinations.items():
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(contents[name])
        target.chmod(0o600)
    return len(contents)


def main() -> int:
    """Restore the exact reference inputs needed by the release test suites."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--manifest", type=Path, default=ROOT / "release/inputs-v1.json")
    args = parser.parse_args()
    try:
        count = restore(args.root.resolve(), args.archive, args.manifest)
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError) as error:
        print(f"Release inputs unavailable: {error}", file=sys.stderr)
        return 1
    print(f"Verified {count} external release inputs; all remain untracked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
