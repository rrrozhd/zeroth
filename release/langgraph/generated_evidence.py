"""Cross-validation for generated image, test, SBOM, and attestation evidence."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from langgraph_benchmark import CURRENT_RELEASE

REQUIRED_TESTCASES = {
    (
        "tests.langgraph_release.test_benchmark",
        "test_benchmark_records_release_metrics_and_rejects_regression",
    ),
    (
        "tests.langgraph_release.test_container_contract",
        "test_container_and_compatibility_contract",
    ),
    (
        "tests.langgraph_release.test_demo",
        "test_release_demo_proves_governance_and_causality",
    ),
    (
        "tests.langgraph_release.test_release_checklist",
        "test_final_release_evidence_binds_generated_artifacts",
    ),
    (
        "tests.docs.test_langgraph_release_docs",
        "test_canonical_guide_covers_release_operations_and_commands_execute",
    ),
}
PACKAGE_KEYS = {
    "zeroth-core": "zeroth_core",
    "langchain": "langchain",
    "langgraph": "langgraph",
    "langgraph-checkpoint-sqlite": "langgraph_checkpoint_sqlite",
    "langgraph-sdk": "langgraph_sdk",
    "httpx": "httpx",
    "websockets": "websockets",
}
LABEL_KEYS = {
    "org.opencontainers.image.version": "zeroth_core",
    "io.zeroth.langgraph.adapter.version": "adapter_version",
    "io.zeroth.langgraph.compatibility.langgraph": "langgraph",
    "io.zeroth.langgraph.compatibility.agent-server": "agent_server",
}


def _json_file(path: Path, label: str, errors: list[str]) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        errors.append(f"{label} unreadable: {error}")
        return None
    if not isinstance(value, dict) or not value:
        errors.append(f"{label} must be a nonempty JSON object")
        return None
    return value


def _json_array(path: Path, label: str, errors: list[str]) -> list[Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        errors.append(f"{label} unreadable: {error}")
        return None
    if not isinstance(value, list) or not value:
        errors.append(f"{label} must be a nonempty JSON array")
        return None
    return value


def _sha256(value: Any) -> bool:
    return re.fullmatch(r"sha256:[0-9a-f]{64}", str(value)) is not None


def _validate_images(path: Path, errors: list[str]) -> dict[str, str] | None:
    value = _json_file(path, "image compatibility evidence", errors)
    images = value.get("images") if value else None
    artifact = value.get("artifact") if value else None
    valid_entries = (
        isinstance(images, list)
        and len(images) == 3
        and all(
            isinstance(image, dict)
            and set(image) == {"reference", "id", "digest", "repo_digests"}
            and _sha256(image.get("id"))
            and _sha256(image.get("digest"))
            and isinstance(image.get("repo_digests"), list)
            for image in images or []
        )
    )
    references = {str(image["reference"]) for image in images or [] if isinstance(image, dict)}
    application = [
        item for item in images or [] if str(item.get("reference", "")).startswith("zeroth-core:")
    ]
    expected_bases = {"python:3.12.13-slim-bookworm", "postgres:16.9-bookworm"}
    valid_artifact = (
        isinstance(artifact, dict)
        and set(artifact) == {"path", "digest"}
        and artifact.get("path") == "zeroth-core-image.tar"
        and _sha256(artifact.get("digest"))
    )
    if value is None or (
        set(value) != {"schema_version", "release", "artifact", "images"}
        or value.get("schema_version") != 2
        or value.get("release") != CURRENT_RELEASE
        or not valid_artifact
        or not valid_entries
        or not expected_bases.issubset(references)
        or len(application) != 1
        or application[0]["reference"].removeprefix("zeroth-core:").removeprefix("v")
        != CURRENT_RELEASE
    ):
        errors.append("image compatibility evidence is invalid")
        return None
    return {
        "reference": str(application[0]["reference"]),
        "id": str(application[0]["id"]),
        "digest": str(application[0]["digest"]),
        "artifact_path": str(artifact["path"]),
        "artifact_digest": str(artifact["digest"]),
    }


def _expected_packages(compatibility: dict[str, Any]) -> dict[str, str]:
    resolved = compatibility.get("resolved", {})
    return {name: str(resolved.get(key, "")) for name, key in PACKAGE_KEYS.items()}


def _expected_labels(compatibility: dict[str, Any]) -> dict[str, str]:
    resolved = compatibility.get("resolved", {})
    values = {**resolved, "adapter_version": compatibility.get("adapter_version")}
    return {name: str(values.get(key, "")) for name, key in LABEL_KEYS.items()}


def _validate_packages(
    path: Path,
    compatibility: dict[str, Any],
    image: dict[str, str] | None,
    errors: list[str],
) -> None:
    value = _json_file(path, "installed image package evidence", errors)
    expected_image = {"reference": image["reference"], "digest": image["digest"]} if image else None
    if value is None or (
        set(value) != {"schema_version", "release", "image", "packages", "labels"}
        or value.get("schema_version") != 1
        or value.get("release") != CURRENT_RELEASE
        or value.get("image") != expected_image
        or value.get("packages") != _expected_packages(compatibility)
        or value.get("labels") != _expected_labels(compatibility)
    ):
        errors.append("installed image packages do not match compatibility evidence")


def _purl(package: dict[str, Any], expected: str) -> bool:
    return any(
        isinstance(reference, dict)
        and reference.get("referenceType") == "purl"
        and reference.get("referenceLocator") == expected
        for reference in package.get("externalRefs", [])
    )


def _spdx_packages(
    packages: list[Any], image: dict[str, str]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    roots = [
        item
        for item in packages
        if isinstance(item, dict)
        and item.get("name") == image["reference"]
        and item.get("primaryPackagePurpose") == "CONTAINER"
    ]
    applications = [
        item
        for item in packages
        if isinstance(item, dict)
        and item.get("name") == "zeroth-core"
        and item.get("versionInfo") == CURRENT_RELEASE
    ]
    return roots[0] if len(roots) == 1 else {}, applications


def _validate_spdx(path: Path, image: dict[str, str] | None, errors: list[str]) -> None:
    value = _json_file(path, "SPDX JSON", errors)
    packages = value.get("packages") if value else None
    if value is None or image is None or not isinstance(packages, list):
        errors.append("SPDX is not bound to the built image and release package")
        return
    digest = image["digest"].removeprefix("sha256:")
    root, applications = _spdx_packages(packages, image)
    checksum = {
        item.get("algorithm"): item.get("checksumValue") for item in root.get("checksums", [])
    }
    describes = any(
        item.get("spdxElementId") == "SPDXRef-DOCUMENT"
        and item.get("relatedSpdxElement") == root.get("SPDXID")
        and item.get("relationshipType") == "DESCRIBES"
        for item in value.get("relationships", [])
        if isinstance(item, dict)
    )
    creators = value.get("creationInfo", {}).get("creators", [])
    valid = (
        value.get("spdxVersion") == "SPDX-2.3"
        and value.get("dataLicense") == "CC0-1.0"
        and value.get("SPDXID") == "SPDXRef-DOCUMENT"
        and value.get("name") == image["reference"]
        and bool(value.get("documentNamespace"))
        and any(str(item).startswith("Tool: syft-") for item in creators)
        and root.get("versionInfo") == image["digest"]
        and checksum.get("SHA256") == digest
        and any(
            f"@sha256%3A{digest}" in str(ref.get("referenceLocator"))
            for ref in root.get("externalRefs", [])
        )
        and describes
        and len(applications) == 1
        and _purl(applications[0], f"pkg:pypi/zeroth-core@{CURRENT_RELEASE}")
    )
    if not valid:
        errors.append("SPDX is not bound to the built image and release package")


def _verified_result(entry: Any, bundle: dict[str, Any], subject: list[dict[str, Any]]) -> bool:
    if not isinstance(entry, dict):
        return False
    attestation = entry.get("attestation")
    if not isinstance(attestation, dict) or attestation.get("bundle") != bundle:
        return False
    result = entry.get("verificationResult")
    if not isinstance(result, dict):
        return False
    signature = result.get("signature")
    statement = result.get("statement")
    return (
        isinstance(signature, dict)
        and bool(signature.get("certificate"))
        and bool(result.get("verifiedTimestamps"))
        and isinstance(statement, dict)
        and statement.get("_type") == "https://in-toto.io/Statement/v1"
        and statement.get("subject") == subject
        and statement.get("predicateType") == "https://slsa.dev/provenance/v1"
        and isinstance(statement.get("predicate"), dict)
        and bool(statement["predicate"])
    )


def _validate_receipt(
    path: Path,
    bundle: dict[str, Any] | None,
    image: dict[str, str] | None,
    errors: list[str],
) -> None:
    value = _json_array(path, "attestation verification receipt", errors)
    if value is None or bundle is None or image is None:
        errors.append("attestation verification receipt is not bound to release evidence")
        return
    subject = [
        {
            "name": image["artifact_path"],
            "digest": {"sha256": image["artifact_digest"].removeprefix("sha256:")},
        }
    ]
    if not any(_verified_result(entry, bundle, subject) for entry in value):
        errors.append("verification receipt does not prove the verified artifact digest")


def _validate_junit(path: Path, errors: list[str]) -> None:
    try:
        root = ElementTree.parse(path).getroot()
    except (OSError, ElementTree.ParseError) as error:
        errors.append(f"test evidence is not JUnit XML: {error}")
        return
    suites = [root] if root.tag == "testsuite" else list(root.findall(".//testsuite"))
    try:
        tests = sum(int(suite.get("tests", "0")) for suite in suites)
        failures = sum(int(suite.get("failures", "0")) for suite in suites)
        error_count = sum(int(suite.get("errors", "0")) for suite in suites)
        skipped = sum(int(suite.get("skipped", "0")) for suite in suites)
    except ValueError:
        tests, failures, error_count, skipped = 0, 1, 1, 1
    identities = {
        (str(case.get("classname")), str(case.get("name"))) for case in root.findall(".//testcase")
    }
    # A required test that was collected but never executed proves nothing, so a
    # skip is as disqualifying as a failure. Both the suite-level counter and the
    # per-identity element are checked: the counter alone can be under-reported by
    # a hand-edited or partially-written document, and the element alone misses a
    # suite that skipped a test it never emitted a ``testcase`` for.
    skipped_identities = {
        (str(case.get("classname")), str(case.get("name")))
        for case in root.findall(".//testcase")
        if case.find("skipped") is not None
    }
    if (
        tests < len(REQUIRED_TESTCASES)
        or failures
        or error_count
        or skipped
        or not any(suite.get("name") == "pytest" for suite in suites)
        or not REQUIRED_TESTCASES.issubset(identities)
        or skipped_identities & REQUIRED_TESTCASES
    ):
        errors.append("JUnit does not prove the expected release test identities passed")


def validate_generated(
    evidence_root: Path,
    security_paths: list[str],
    compatibility: dict[str, Any],
    junit_path: str,
) -> list[str]:
    """Return cross-artifact errors for final release evidence."""
    errors: list[str] = []
    spdx_path, bundle_path, receipt_path, image_path, packages_path = security_paths
    image = _validate_images(evidence_root / image_path, errors)
    bundle = _json_file(evidence_root / bundle_path, "Sigstore bundle", errors)
    _validate_receipt(evidence_root / receipt_path, bundle, image, errors)
    _validate_packages(evidence_root / packages_path, compatibility, image, errors)
    _validate_spdx(evidence_root / spdx_path, image, errors)
    _validate_junit(evidence_root / junit_path, errors)
    return errors
