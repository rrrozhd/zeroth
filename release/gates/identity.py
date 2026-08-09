"""Derive a release candidate's identity from the candidate itself.

Evidence is only worth anything when it is bound to the exact artifact being
released. Every value produced here is *measured* -- read out of the working
tree or hashed from a built artifact -- so changing the candidate necessarily
changes its identity.

This is deliberately the opposite of a hand-maintained release constant. A
constant that names the release can drift from the thing actually being
released, and then evidence validates itself instead of the candidate.

The ticket names five identity facets: commit, package, image, configuration,
and compatibility. They are modelled here one-to-one.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tomllib
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DIGEST_PREFIX = "sha256:"

#: The identity facets a gate record may bind itself to.
FACETS = ("commit", "package", "image", "configuration", "compatibility")

_CHUNK = 1 << 20


class IdentityError(RuntimeError):
    """A candidate identity could not be measured."""


def canonical(value: Any) -> bytes:
    """Return the canonical byte encoding used for every digest here."""
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value: Any) -> str:
    """Return the prefixed sha256 of ``value``'s canonical encoding."""
    return DIGEST_PREFIX + hashlib.sha256(canonical(value)).hexdigest()


def file_digest(path: Path) -> str:
    """Return the prefixed sha256 of a file's bytes."""
    if not path.is_file():
        raise IdentityError(f"cannot digest missing artifact: {path}")
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            sha.update(chunk)
    return DIGEST_PREFIX + sha.hexdigest()


def head_commit(root: Path) -> str:
    """Return the working tree's HEAD commit."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:  # pragma: no cover
        raise IdentityError(f"cannot read HEAD commit: {error}") from error
    return completed.stdout.strip()


def project_version(root: Path) -> str:
    """Return ``[project].version`` as declared by ``pyproject.toml``."""
    path = root / "pyproject.toml"
    try:
        with path.open("rb") as handle:
            parsed = tomllib.load(handle)
        return str(parsed["project"]["version"])
    except (OSError, KeyError, tomllib.TOMLDecodeError) as error:
        raise IdentityError(f"cannot read project version: {error}") from error


def _artifact_digests(artifacts: dict[str, Path]) -> dict[str, str]:
    return {name: file_digest(path) for name, path in sorted(artifacts.items())}


def candidate_identity(
    root: Path,
    *,
    commit: str | None = None,
    artifacts: dict[str, Path] | None = None,
    image: dict[str, str] | None = None,
    configuration: Path | None = None,
    compatibility: Path | None = None,
) -> dict[str, Any]:
    """Measure the candidate under release and return its identity.

    Args:
        root: Repository root of the candidate.
        commit: Override for the commit facet; defaults to the measured HEAD.
        artifacts: Built package artifacts to hash, keyed by a stable name.
        image: Resolved image references mapped to their registry digests.
        configuration: File whose bytes stand for the configuration surface.
        compatibility: File whose bytes stand for the resolved compatibility set.

    Returns:
        The identity object. Facets that were not supplied are absent rather
        than null, so a gate requiring a facet fails closed instead of matching
        a placeholder.
    """
    identity: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "commit": commit or head_commit(root),
        "package": {
            "version": project_version(root),
            "artifacts": _artifact_digests(artifacts or {}),
        },
    }
    if image:
        identity["image"] = dict(sorted(image.items()))
    if configuration is not None:
        identity["configuration"] = file_digest(configuration)
    if compatibility is not None:
        identity["compatibility"] = file_digest(compatibility)
    return identity


def identity_digest(identity: dict[str, Any]) -> str:
    """Return the single digest that stands for a whole candidate identity."""
    return digest(identity)


def facet_matches(candidate: dict[str, Any], record: dict[str, Any], facet: str) -> bool:
    """Return whether ``record`` binds ``facet`` to the same value as ``candidate``."""
    if facet not in candidate or facet not in record:
        return False
    return canonical(candidate[facet]) == canonical(record[facet])
