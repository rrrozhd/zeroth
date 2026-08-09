"""R1 — the candidate identity is measured from the candidate, never declared."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from .conftest import RELEASE, ROOT


def test_identity_reports_the_version_the_repository_actually_declares():
    import tomllib

    from gates.identity import candidate_identity

    with (ROOT / "pyproject.toml").open("rb") as handle:
        declared = tomllib.load(handle)["project"]["version"]

    assert candidate_identity(ROOT)["package"]["version"] == declared


def test_identity_reports_the_commit_the_repository_is_actually_on():
    from gates.identity import candidate_identity

    head = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert candidate_identity(ROOT)["commit"] == head


def test_changing_artifact_bytes_changes_the_identity(tmp_path: Path):
    from gates.identity import candidate_identity, identity_digest

    wheel = tmp_path / "zeroth_core-0.19-py3-none-any.whl"
    wheel.write_bytes(b"first build")
    before = identity_digest(candidate_identity(ROOT, artifacts={"wheel": wheel}))
    wheel.write_bytes(b"second build")
    after = identity_digest(candidate_identity(ROOT, artifacts={"wheel": wheel}))

    assert before != after


@pytest.mark.parametrize(
    ("facet", "first", "second"),
    [
        ("commit", "a" * 40, "b" * 40),
    ],
)
def test_changing_the_commit_changes_the_identity(facet, first, second):
    from gates.identity import candidate_identity, identity_digest

    before = identity_digest(candidate_identity(ROOT, commit=first))
    after = identity_digest(candidate_identity(ROOT, commit=second))

    assert before != after


def test_changing_the_image_digest_changes_the_identity():
    from gates.identity import candidate_identity, identity_digest

    before = candidate_identity(ROOT, image={"zeroth-core:v0.19": "sha256:" + "1" * 64})
    after = candidate_identity(ROOT, image={"zeroth-core:v0.19": "sha256:" + "2" * 64})

    assert identity_digest(before) != identity_digest(after)


def test_absent_facets_are_absent_rather_than_null():
    """A null facet would compare equal to another null one and pass by accident."""
    from gates.identity import candidate_identity

    identity = candidate_identity(ROOT)

    assert "image" not in identity
    assert "configuration" not in identity
    assert "compatibility" not in identity


def test_a_missing_artifact_is_an_error_not_a_silently_empty_digest(tmp_path: Path):
    from gates.identity import IdentityError, candidate_identity

    with pytest.raises(IdentityError):
        candidate_identity(ROOT, artifacts={"wheel": tmp_path / "never-built.whl"})


def test_file_digest_matches_the_bytes_on_disk(tmp_path: Path):
    import hashlib

    from gates.identity import file_digest

    artifact = tmp_path / "artifact.bin"
    payload = b"zeroth" * 5000
    artifact.write_bytes(payload)

    assert file_digest(artifact) == "sha256:" + hashlib.sha256(payload).hexdigest()


def test_facet_matches_requires_the_facet_on_both_sides():
    from gates.identity import facet_matches

    assert facet_matches({"commit": "x"}, {"commit": "x"}, "commit")
    assert not facet_matches({"commit": "x"}, {"commit": "y"}, "commit")
    assert not facet_matches({"commit": "x"}, {}, "commit")
    assert not facet_matches({}, {"commit": "x"}, "commit")


def test_the_gate_substrate_hardcodes_no_release_version():
    """The defect this ticket fixes, kept fixed.

    ``release/langgraph/release_evidence.py`` validates a manifest whose
    ``release`` is the hand-edited constant ``CURRENT_RELEASE``. When the real
    project version moved on, that check kept passing -- evidence validating
    itself instead of the candidate. Nothing under ``release/gates`` may name a
    release version, so it can only ever compare against a measured one.
    """
    import re

    version_like = re.compile(r"\b0\.\d+\.\d+")
    offenders = []
    for path in sorted((RELEASE / "gates").rglob("*")):
        if path.suffix not in {".py", ".json"} or "__pycache__" in path.parts:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if version_like.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")

    assert not offenders, "release version literals in the gate substrate:\n" + "\n".join(offenders)
