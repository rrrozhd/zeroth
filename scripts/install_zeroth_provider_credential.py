#!/usr/bin/env python3
"""Install a rotated provider credential outside the repository without echoing it."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
from collections.abc import Sequence
from pathlib import Path

_CREDENTIAL = re.compile(r"[A-Za-z0-9_-]{20,4096}")


def _validated_destination(destination: Path, *, repository_root: Path) -> Path:
    repository = repository_root.expanduser().resolve(strict=True)
    raw = destination.expanduser()
    if raw.is_symlink():
        raise ValueError("credential destination must not be a symlink")
    resolved = raw.resolve(strict=False)
    if resolved == repository or repository in resolved.parents:
        raise ValueError("credential destination must remain outside the repository")
    return resolved


def _validated_credential(value: str) -> str:
    if not isinstance(value, str) or _CREDENTIAL.fullmatch(value) is None:
        raise ValueError("provider credential has an invalid shape")
    return value


def install_credential(
    *,
    destination: Path,
    repository_root: Path,
    credential: str,
) -> dict[str, object]:
    """Create a private, external env file exactly once."""
    target = _validated_destination(destination, repository_root=repository_root)
    secret = _validated_credential(credential)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target.parent, 0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(f"OPENAI_API_KEY={secret}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(target, 0o600)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return {
        "destination": str(target),
        "mode": "0600",
        "provider": "openai",
        "secret_persisted": True,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--repository-root", required=True, type=Path)
    args = parser.parse_args(argv)
    credential = getpass.getpass("New rotated OpenAI credential (input hidden): ")
    result = install_credential(
        destination=args.destination,
        repository_root=args.repository_root,
        credential=credential,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
