"""The container's declared version is single-sourced, and its drift is recorded.

``Dockerfile``'s ``org.opencontainers.image.version``, ``docker-compose.yml``'s
default image tag and ``compatibility.json``'s release all name the same thing:
the release the LangGraph evidence set describes. The container-contract test
asserted that value as a *literal*, so the three could not drift from each other
-- and could drift from the package version indefinitely without anything
noticing. Across the 27 commits before ZER-41 the package version moved twice
more while the label stayed at 0.17.0.4, and the test stayed green. That is the
freeze, observed.

Two changes. The literal is derived now, so a Dockerfile edit that moves the
label without moving the evidence identity fails. And the gap between the
evidence identity and the package version is *declared* below, with the reason
and the condition that clears it, so a freeze is a recorded fact instead of an
invisible one (ZER-41 / A14-10).
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: Evidence releases that trail the package version, and why.
#:
#: The LangGraph evidence set -- benchmark, compatibility matrix, SBOM, package
#: inventory -- was *measured* against one release. Advancing the identity without
#: re-measuring would relabel that evidence as describing a build it never saw,
#: which is exactly the fabrication the release gates exist to refuse. So the gap
#: is recorded rather than closed by editing a constant.
#:
#: **This record may only shrink.** An entry clears when the evidence set is
#: regenerated for the current version: re-run the benchmark, rebuild the image,
#: re-derive the SBOM, then delete the line.
STALE_EVIDENCE_RELEASES = {
    "0.17.0.4": (
        "benchmark-evidence.json, compatibility.json and the image inventory were "
        "measured against 0.17.0.4; regenerating them is a release operation, not "
        "an edit."
    ),
}


def _package_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]


def _evidence_release() -> str:
    return json.loads(
        (ROOT / "release/langgraph/compatibility.json").read_text(encoding="utf-8")
    )["release"]


def _dockerfile_label() -> str:
    source = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    match = re.search(r"org\.opencontainers\.image\.version=(\S+)", source)
    assert match, "Dockerfile declares no org.opencontainers.image.version"
    return match.group(1)


def _compose_default_tag() -> str:
    source = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    match = re.search(r"image:\s*zeroth-core:\$\{ZEROTH_IMAGE_TAG:-([^}]+)\}", source)
    assert match, "docker-compose.yml declares no default zeroth-core tag"
    return match.group(1)


def test_the_container_version_is_declared_once_and_agrees_everywhere() -> None:
    """Three declarations, one value -- checked, not repeated as three literals.

    A Dockerfile edit that moved the label without moving the evidence identity
    used to pass, because the test asserted the same literal the Dockerfile did.
    """
    evidence = _evidence_release()

    assert _dockerfile_label() == evidence
    assert _compose_default_tag() == evidence


def test_the_running_package_version_matches_the_label_or_the_gap_is_recorded() -> None:
    """A container may not silently claim a version it does not ship.

    Measured at the audit: the label read 0.17.0.4 while the wheel installed in
    the image was 0.22.1.1.1, and the only test covering it asserted the literal.
    A consumer reading the OCI label to identify the software would have been
    wrong by five releases with nothing reporting it.
    """
    package = _package_version()
    evidence = _evidence_release()

    if package == evidence:
        assert evidence not in STALE_EVIDENCE_RELEASES, (
            f"{evidence} is recorded as stale but now matches the package version; "
            "delete its entry from STALE_EVIDENCE_RELEASES"
        )
        return

    assert evidence in STALE_EVIDENCE_RELEASES, (
        f"the container declares {evidence} while the package is {package}, and the "
        "gap is not recorded in STALE_EVIDENCE_RELEASES"
    )
    assert STALE_EVIDENCE_RELEASES[evidence].strip(), evidence


def test_the_stale_record_names_no_release_that_is_not_stale() -> None:
    """A stale entry would let a real drift hide behind a retired one."""
    package = _package_version()

    assert package not in STALE_EVIDENCE_RELEASES


def test_the_drift_detector_sees_a_moved_label() -> None:
    """The parsers, fed the edit they exist to catch.

    Without this the assertions above could hold because both helpers return the
    same wrong thing, which is the failure mode a literal assertion had.
    """
    moved = re.sub(
        r"org\.opencontainers\.image\.version=\S+",
        "org.opencontainers.image.version=9.9.9",
        (ROOT / "Dockerfile").read_text(encoding="utf-8"),
    )
    match = re.search(r"org\.opencontainers\.image\.version=(\S+)", moved)

    assert match is not None
    assert match.group(1) == "9.9.9"
    assert match.group(1) != _evidence_release()


def test_a_manifest_that_disagrees_with_the_requirement_set_fails() -> None:
    """The manifest is validated, not followed -- and disagreement is caught.

    Every artifact ``validate_manifest`` checks resolves from the module-level
    ``REQUIRED_EVIDENCE`` dict or a literal path; the ``manifest`` binding is read
    by one schema check and never again. So the file cannot express anything the
    validator does not already hardcode. That is deliberate -- a manifest that
    drove resolution would let a candidate choose the evidence it is judged on --
    but it means the file's only power is to disagree, and disagreeing must fail.
    """
    import importlib
    import tempfile

    release_evidence = importlib.import_module("release.langgraph.release_evidence")
    source = ROOT / "release/langgraph/release-manifest.json"

    assert release_evidence.validate_manifest(source, phase="source") == []

    manifest = json.loads(source.read_text(encoding="utf-8"))
    manifest["evidence"]["performance"]["artifacts"].append("release/langgraph/invented.json")
    with tempfile.TemporaryDirectory() as directory:
        edited = Path(directory) / "release-manifest.json"
        edited.write_text(json.dumps(manifest), encoding="utf-8")

        errors = release_evidence.validate_manifest(edited, phase="source")

    assert errors == ["manifest schema or release is invalid"]


def test_the_release_guide_does_not_present_resolution_as_manifest_driven() -> None:
    """The docs said "manifest" where the code says "hardcoded requirement set"."""
    guide = (ROOT / "docs/how-to/deployment/langgraph-release.md").read_text(encoding="utf-8")

    assert "validated, not followed" in guide
    assert "REQUIRED_EVIDENCE" in guide
