from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "release/langgraph/harness.py"
MANIFEST = ROOT / "release/langgraph/release-manifest.json"
COMPATIBILITY = json.loads(
    (ROOT / "release/langgraph/compatibility.json").read_text(encoding="utf-8")
)
RELEASE = COMPATIBILITY["release"]
IMAGE_REFERENCE = f"zeroth-core:v{RELEASE}"
IMAGE_ID = "sha256:" + "a" * 64
# The application image is built and not pushed before evidence generation, so
# the daemon reports no registry digest for it and its digest *is* its config id.
IMAGE_DIGEST = IMAGE_ID
ARCHIVE_DIGEST = "sha256:" + "e" * 64
REQUIRED_TESTS = {
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


def _validate(manifest: Path, evidence_root: Path, phase: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            HARNESS,
            "validate",
            "--manifest",
            manifest,
            "--evidence-root",
            evidence_root,
            "--phase",
            phase,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _validate_default(manifest: Path, evidence_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            HARNESS,
            "validate",
            "--manifest",
            manifest,
            "--evidence-root",
            evidence_root,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _source_tree(tmp_path: Path) -> tuple[Path, Path]:
    evidence_root = tmp_path / "evidence"
    release = evidence_root / "release/langgraph"
    release.mkdir(parents=True)
    for name in (
        "benchmark-baseline-0.16.1.7.json",
        "benchmark-evidence.json",
        "compatibility.json",
        "release-manifest.json",
    ):
        shutil.copy2(ROOT / "release/langgraph" / name, release / name)
    return evidence_root, release / "release-manifest.json"


def _junit_xml() -> str:
    cases = "".join(
        f'<testcase classname="{classname}" name="{name}"/>'
        for classname, name in sorted(REQUIRED_TESTS)
    )
    count = len(REQUIRED_TESTS)
    return (
        f'<testsuites name="pytest tests"><testsuite name="pytest" tests="{count}" '
        f'failures="0" errors="0">{cases}</testsuite></testsuites>'
    )


# Source: https://github.com/anchore/syft/blob/main/syft/format/spdxjson/testdata/snapshot/TestSPDXJSONImageEncoder.golden
def _syft_spdx_golden_fragment() -> dict[str, object]:
    digest = IMAGE_DIGEST.removeprefix("sha256:")
    root_id = "SPDXRef-DocumentRoot-Image-zeroth-core"
    return {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": IMAGE_REFERENCE,
        "documentNamespace": "https://anchore.com/syft/image/zeroth-core-test-golden",
        "creationInfo": {"creators": ["Organization: Anchore, Inc", "Tool: syft-v0.42.0-bogus"]},
        "packages": [
            {
                "name": IMAGE_REFERENCE,
                "SPDXID": root_id,
                "versionInfo": IMAGE_DIGEST,
                "primaryPackagePurpose": "CONTAINER",
                "checksums": [{"algorithm": "SHA256", "checksumValue": digest}],
                "externalRefs": [
                    {
                        "referenceCategory": "PACKAGE-MANAGER",
                        "referenceType": "purl",
                        "referenceLocator": f"pkg:oci/zeroth-core@sha256%3A{digest}",
                    }
                ],
            },
            {
                "name": "zeroth-core",
                "SPDXID": "SPDXRef-Package-python-zeroth-core",
                "versionInfo": RELEASE,
                "externalRefs": [
                    {
                        "referenceCategory": "PACKAGE-MANAGER",
                        "referenceType": "purl",
                        "referenceLocator": f"pkg:pypi/zeroth-core@{RELEASE}",
                    }
                ],
            },
        ],
        "relationships": [
            {
                "spdxElementId": "SPDXRef-DOCUMENT",
                "relatedSpdxElement": root_id,
                "relationshipType": "DESCRIBES",
            }
        ],
    }


def _attestation_bundle() -> dict[str, object]:
    return {"mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json"}


def _verification_receipt(digest: str = ARCHIVE_DIGEST) -> list[dict[str, object]]:
    return [
        {
            "attestation": {
                "bundle": _attestation_bundle(),
                "bundle_url": "",
                "initiator": "",
            },
            "verificationResult": {
                "signature": {"certificate": {"sourceRepository": "zeroth-core"}},
                "verifiedTimestamps": [{"type": "transparency-log"}],
                "statement": {
                    "_type": "https://in-toto.io/Statement/v1",
                    "subject": [
                        {
                            "name": "zeroth-core-image.tar",
                            "digest": {"sha256": digest.removeprefix("sha256:")},
                        }
                    ],
                    "predicateType": "https://slsa.dev/provenance/v1",
                    "predicate": {"buildDefinition": {}, "runDetails": {}},
                },
            },
        }
    ]


def _image_packages() -> dict[str, object]:
    resolved = COMPATIBILITY["resolved"]
    packages = {
        "zeroth-core": RELEASE,
        "langchain": resolved["langchain"],
        "langgraph": resolved["langgraph"],
        "langgraph-checkpoint-sqlite": resolved["langgraph_checkpoint_sqlite"],
        "langgraph-sdk": resolved["langgraph_sdk"],
        "httpx": "0.28.1",
        "websockets": "15.0.1",
    }
    return {
        "schema_version": 1,
        "release": RELEASE,
        "image": {"reference": IMAGE_REFERENCE, "digest": IMAGE_DIGEST},
        "packages": packages,
        "labels": {
            "org.opencontainers.image.version": RELEASE,
            "io.zeroth.langgraph.adapter.version": COMPATIBILITY["adapter_version"],
            "io.zeroth.langgraph.compatibility.langgraph": resolved["langgraph"],
            "io.zeroth.langgraph.compatibility.agent-server": resolved["agent_server"],
        },
    }


def _write_generated_evidence(evidence_root: Path) -> None:
    release = evidence_root / "release/langgraph"
    payloads = {
        "image.spdx.json": _syft_spdx_golden_fragment(),
        "provenance.bundle.json": _attestation_bundle(),
        "attestation-verification.json": _verification_receipt(),
        "image-compatibility.json": {
            "schema_version": 2,
            "release": RELEASE,
            "artifact": {
                "path": "zeroth-core-image.tar",
                "digest": ARCHIVE_DIGEST,
            },
            # Every digest here is now tied to a field `docker image inspect`
            # produced. The base images carry the registry digest they were pulled
            # by; before this the fixture recorded them with `repo_digests: []`
            # and an arbitrary digest, and validation accepted it -- so the gate
            # would have accepted a base-image digest belonging to no registry.
            "images": [
                {
                    "reference": IMAGE_REFERENCE,
                    "id": IMAGE_ID,
                    "digest": IMAGE_DIGEST,
                    "repo_digests": [],
                },
                {
                    "reference": "python:3.12.13-slim-bookworm",
                    "id": "sha256:" + "b" * 64,
                    "digest": "sha256:" + "b" * 64,
                    "repo_digests": ["python@sha256:" + "b" * 64],
                },
                {
                    "reference": "postgres:16.9-bookworm",
                    "id": "sha256:" + "c" * 64,
                    "digest": "sha256:" + "c" * 64,
                    "repo_digests": ["postgres@sha256:" + "c" * 64],
                },
            ],
        },
        "image-packages.json": _image_packages(),
    }
    (release / "junit.xml").write_text(_junit_xml(), encoding="utf-8")
    for name, payload in payloads.items():
        (release / name).write_text(json.dumps(payload), encoding="utf-8")


def test_release_validation_defaults_to_final_evidence(tmp_path: Path) -> None:
    evidence_root, manifest = _source_tree(tmp_path)
    result = _validate_default(manifest, evidence_root)
    assert result.returncode != 0
    assert "SPDX JSON" in result.stderr


def test_source_release_evidence_rejects_empty_stale_and_failed_inputs(tmp_path: Path) -> None:
    evidence_root, manifest = _source_tree(tmp_path)
    assert _validate(manifest, evidence_root, "source").returncode == 0

    compatibility = evidence_root / "release/langgraph/compatibility.json"
    compatibility.write_text("{}", encoding="utf-8")
    result = _validate(manifest, evidence_root, "source")
    assert result.returncode != 0
    assert "compatibility schema" in result.stderr

    shutil.copy2(ROOT / "release/langgraph/compatibility.json", compatibility)
    benchmark = evidence_root / "release/langgraph/benchmark-evidence.json"
    payload = json.loads(benchmark.read_text(encoding="utf-8"))
    payload["release"] = "0.16.2"
    benchmark.write_text(json.dumps(payload), encoding="utf-8")
    result = _validate(manifest, evidence_root, "source")
    assert result.returncode != 0
    assert "performance release" in result.stderr

    payload["release"] = RELEASE
    payload["passed"] = False
    payload["evaluation"]["ttft_p95_ms"] = False
    benchmark.write_text(json.dumps(payload), encoding="utf-8")
    result = _validate(manifest, evidence_root, "source")
    assert result.returncode != 0
    assert "performance evidence did not pass" in result.stderr


def test_source_release_evidence_rejects_weakened_contracts(tmp_path: Path) -> None:
    evidence_root, manifest = _source_tree(tmp_path)
    compatibility = evidence_root / "release/langgraph/compatibility.json"
    benchmark = evidence_root / "release/langgraph/benchmark-evidence.json"

    compatibility_payload = json.loads(compatibility.read_text(encoding="utf-8"))
    compatibility_payload["deployment_artifacts"]["adapter"]["dependencies"] = ["langchain>=1.0,<2"]
    compatibility.write_text(json.dumps(compatibility_payload), encoding="utf-8")
    result = _validate(manifest, evidence_root, "source")
    assert result.returncode != 0
    assert "compatibility schema" in result.stderr

    shutil.copy2(ROOT / "release/langgraph/compatibility.json", compatibility)
    benchmark_payload = json.loads(benchmark.read_text(encoding="utf-8"))
    benchmark_payload["thresholds"]["rules"]["ttft_p95_ms"]["maximum"] = 1_000_000
    benchmark.write_text(json.dumps(benchmark_payload), encoding="utf-8")
    result = _validate(manifest, evidence_root, "source")
    assert result.returncode != 0
    assert "performance evidence schema" in result.stderr

    shutil.copy2(ROOT / "release/langgraph/benchmark-evidence.json", benchmark)
    benchmark_payload = json.loads(benchmark.read_text(encoding="utf-8"))
    benchmark_payload["sample_count"] = 3
    benchmark_payload["sample_distribution"] = {
        name: samples[:3] for name, samples in benchmark_payload["sample_distribution"].items()
    }
    benchmark.write_text(json.dumps(benchmark_payload), encoding="utf-8")
    result = _validate(manifest, evidence_root, "source")
    assert result.returncode != 0
    assert "performance evidence schema" in result.stderr


def _final_tree(tmp_path: Path) -> tuple[Path, Path]:
    evidence_root, manifest = _source_tree(tmp_path)
    _write_generated_evidence(evidence_root)
    return evidence_root, manifest


def test_final_release_evidence_binds_generated_artifacts(tmp_path: Path) -> None:
    evidence_root, manifest = _final_tree(tmp_path)
    assert _validate(manifest, evidence_root, "final").returncode == 0


def test_final_release_evidence_rejects_wrong_junit_identity(tmp_path: Path) -> None:
    evidence_root, manifest = _final_tree(tmp_path)
    junit = evidence_root / "release/langgraph/junit.xml"
    junit.write_text('<testsuite tests="1" failures="1" errors="0"/>', encoding="utf-8")
    result = _validate(manifest, evidence_root, "final")
    assert result.returncode != 0
    assert "expected release test identities" in result.stderr

    junit.write_text(
        _junit_xml().replace(
            "test_benchmark_records_release_metrics_and_rejects_regression",
            "not_the_expected_release_test",
        ),
        encoding="utf-8",
    )
    result = _validate(manifest, evidence_root, "final")
    assert result.returncode != 0
    assert "expected release test identities" in result.stderr


def _junit_with_skips(*skipped: tuple[str, str]) -> str:
    """A JUnit document whose named identities carry a ``<skipped>`` element.

    The suite counter is set from the number of skips, so the document is
    internally consistent -- exactly what a real ``pytest`` run emits when a
    required test is skipped rather than executed.
    """
    marked = set(skipped)
    cases = "".join(
        (
            f'<testcase classname="{classname}" name="{name}">'
            '<skipped type="pytest.skip" message="docker unavailable"/></testcase>'
            if (classname, name) in marked
            else f'<testcase classname="{classname}" name="{name}"/>'
        )
        for classname, name in sorted(REQUIRED_TESTS)
    )
    return (
        f'<testsuites name="pytest tests"><testsuite name="pytest" tests="{len(REQUIRED_TESTS)}" '
        f'failures="0" errors="0" skipped="{len(marked)}">{cases}</testsuite></testsuites>'
    )


def test_final_release_evidence_rejects_skipped_required_tests(tmp_path: Path) -> None:
    """A required test that was skipped never ran, so it cannot certify a release.

    Three documents, each of which validated clean before this gate existed: one
    required identity skipped, all of them skipped, and a suite counter reporting
    a skip with no ``<skipped>`` element to match it.
    """
    evidence_root, manifest = _final_tree(tmp_path)
    junit = evidence_root / "release/langgraph/junit.xml"
    one = sorted(REQUIRED_TESTS)[0]

    junit.write_text(_junit_with_skips(one), encoding="utf-8")
    result = _validate(manifest, evidence_root, "final")
    assert result.returncode != 0
    assert "expected release test identities" in result.stderr

    junit.write_text(_junit_with_skips(*REQUIRED_TESTS), encoding="utf-8")
    result = _validate(manifest, evidence_root, "final")
    assert result.returncode != 0
    assert "expected release test identities" in result.stderr

    junit.write_text(_junit_xml().replace('errors="0"', 'errors="0" skipped="1"'), encoding="utf-8")
    result = _validate(manifest, evidence_root, "final")
    assert result.returncode != 0
    assert "expected release test identities" in result.stderr


def test_final_release_evidence_accepts_the_unskipped_document(tmp_path: Path) -> None:
    """The skip rejection must not have made the honest document unacceptable."""
    evidence_root, manifest = _final_tree(tmp_path)
    junit = evidence_root / "release/langgraph/junit.xml"
    junit.write_text(_junit_with_skips(), encoding="utf-8")

    assert _validate(manifest, evidence_root, "final").returncode == 0


def test_final_release_evidence_rejects_unbound_spdx(tmp_path: Path) -> None:
    evidence_root, manifest = _final_tree(tmp_path)
    path = evidence_root / "release/langgraph/image.spdx.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    relationship = payload["relationships"][0]
    relationship["spdxElementId"], relationship["relatedSpdxElement"] = (
        relationship["relatedSpdxElement"],
        relationship["spdxElementId"],
    )
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = _validate(manifest, evidence_root, "final")
    assert result.returncode != 0
    assert "SPDX is not bound" in result.stderr


def test_final_release_evidence_rejects_a_consistently_tampered_image_digest(
    tmp_path: Path,
) -> None:
    """The SBOM is checked against the image, not against itself.

    Measured before this fix: replacing the application image's digest with an
    entirely different one, *consistently* across ``image.spdx.json``,
    ``image-compatibility.json`` and ``image-packages.json`` and leaving the
    daemon-sourced ``id`` untouched, still returned rc=0 and "release evidence
    complete". Every check compared the SBOM with itself, so agreeing with itself
    was all a forged digest had to do.
    """
    evidence_root, manifest = _final_tree(tmp_path)
    assert _validate(manifest, evidence_root, "final").returncode == 0

    forged = "sha256:" + "9" * 64
    for name in ("image.spdx.json", "image-packages.json"):
        path = evidence_root / "release/langgraph" / name
        body = path.read_text(encoding="utf-8")
        body = body.replace(IMAGE_DIGEST, forged)
        body = body.replace(IMAGE_DIGEST.removeprefix("sha256:"), forged.removeprefix("sha256:"))
        path.write_text(body, encoding="utf-8")

    # Only the digest moves here. `id` is the daemon's own value and the SBOM
    # never supplies it, so leaving it alone is what the described tamper does --
    # and it is the discriminator the binding rests on.
    compatibility_path = evidence_root / "release/langgraph/image-compatibility.json"
    compatibility = json.loads(compatibility_path.read_text(encoding="utf-8"))
    application = next(
        image for image in compatibility["images"] if image["reference"].startswith("zeroth-core:")
    )
    application["digest"] = forged
    assert application["id"] != forged
    compatibility_path.write_text(json.dumps(compatibility), encoding="utf-8")

    result = _validate(manifest, evidence_root, "final")

    assert result.returncode != 0
    assert "image compatibility evidence is invalid" in result.stderr


def test_final_release_evidence_rejects_a_base_image_digest_from_no_registry(
    tmp_path: Path,
) -> None:
    """A base image recorded with no registry digest may not claim an arbitrary one.

    The fixture used to record the postgres base with ``repo_digests: []`` and an
    arbitrary ``sha256:ccc...``, and validation accepted it -- the only check was
    the ``sha256:<64hex>`` shape and tag membership.
    """
    evidence_root, manifest = _final_tree(tmp_path)
    path = evidence_root / "release/langgraph/image-compatibility.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    base = next(
        image for image in payload["images"] if image["reference"].startswith("postgres:")
    )
    base["repo_digests"] = []
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = _validate(manifest, evidence_root, "final")

    assert result.returncode != 0
    assert "image compatibility evidence is invalid" in result.stderr


def test_final_release_evidence_requires_verified_attestation_receipt(
    tmp_path: Path,
) -> None:
    evidence_root, manifest = _final_tree(tmp_path)
    receipt = evidence_root / "release/langgraph/attestation-verification.json"
    if receipt.exists():
        receipt.unlink()
    result = _validate(manifest, evidence_root, "final")
    assert result.returncode != 0
    assert "verification receipt" in result.stderr

    receipt.write_text(json.dumps(_verification_receipt(IMAGE_ID)), encoding="utf-8")
    result = _validate(manifest, evidence_root, "final")
    assert result.returncode != 0
    assert "verified artifact digest" in result.stderr


def test_final_release_evidence_rejects_installed_package_drift(tmp_path: Path) -> None:
    evidence_root, manifest = _final_tree(tmp_path)
    path = evidence_root / "release/langgraph/image-packages.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["packages"]["langgraph"] = "0.0.0"
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = _validate(manifest, evidence_root, "final")
    assert result.returncode != 0
    assert "installed image packages" in result.stderr
