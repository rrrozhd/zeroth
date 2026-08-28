#!/usr/bin/env python3
"""Bind the persistent development service to a measured Git and image identity."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
IMAGE_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT_KEY = "ZEROTH_CERTIFICATION__SERVING_APP_COMMIT"
IMAGE_KEY = "ZEROTH_CERTIFICATION__SERVING_IMAGE_DIGEST"

Runner = Callable[..., subprocess.CompletedProcess[str]]


def measured_identity(
    workspace: Path,
    image: str,
    *,
    runner: Runner = subprocess.run,
) -> tuple[str, str]:
    """Return the clean checkout commit and Docker-owned immutable image ID."""
    status = runner(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status.strip():
        raise RuntimeError("serving identity requires a clean tracked working tree")
    commit = runner(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    digest = runner(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not COMMIT_PATTERN.fullmatch(commit):
        raise RuntimeError("Git did not return a full immutable commit identity")
    if not IMAGE_PATTERN.fullmatch(digest):
        raise RuntimeError("Docker did not return an immutable sha256 image ID")
    return commit, digest


def write_identity(path: Path, commit: str, digest: str) -> None:
    """Atomically replace only the two server-owned identity settings."""
    if not COMMIT_PATTERN.fullmatch(commit) or not IMAGE_PATTERN.fullmatch(digest):
        raise ValueError("invalid serving artifact identity")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    retained: list[str] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.split("=", 1)[0].strip() not in {COMMIT_KEY, IMAGE_KEY}:
                retained.append(line)
    rendered = "\n".join(
        [*retained, f"{COMMIT_KEY}={commit}", f"{IMAGE_KEY}={digest}", ""]
    )
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(rendered)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    os.chmod(path, 0o600)


def main() -> int:
    """Measure the selected image and persist its server configuration."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--env-file", type=Path, default=Path(".dev-secrets/zeroth.env"))
    parser.add_argument("--image", default="zeroth-dev-backend:latest")
    args = parser.parse_args()
    commit, digest = measured_identity(args.workspace.resolve(), args.image)
    write_identity(args.env_file.resolve(), commit, digest)
    print("Configured server-owned commit and image identity.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
