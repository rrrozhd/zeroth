from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "release/langgraph/harness.py"
MANIFEST = ROOT / "release/langgraph/release-manifest.json"


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


def _write_generated_evidence(evidence_root: Path) -> None:
    release = evidence_root / "release/langgraph"
    (release / "junit.xml").write_text(
        '<testsuites tests="1" failures="0" errors="0"><testsuite tests="1"/></testsuites>',
        encoding="utf-8",
    )
    (release / "image.spdx.json").write_text(
        json.dumps(
            {
                "spdxVersion": "SPDX-2.3",
                "name": "zeroth-core-image",
                "documentNamespace": "https://example.invalid/spdx/zeroth-core",
                "packages": [{"name": "zeroth-core", "SPDXID": "SPDXRef-Package"}],
            }
        ),
        encoding="utf-8",
    )
    (release / "provenance.bundle.json").write_text(
        json.dumps(
            {
                "mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json",
                "verificationMaterial": {"certificate": {"rawBytes": "Y2VydA=="}},
                "dsseEnvelope": {"payload": "e30=", "signatures": [{"sig": "c2ln"}]},
            }
        ),
        encoding="utf-8",
    )
    (release / "image-compatibility.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "release": "0.16.2.1",
                "images": [
                    {
                        "reference": "zeroth-core:v0.16.2.1",
                        "id": "sha256:" + "a" * 64,
                        "repo_digests": [],
                    },
                    {
                        "reference": "python:3.12.13-slim-bookworm",
                        "id": "sha256:" + "b" * 64,
                        "repo_digests": [],
                    },
                    {
                        "reference": "postgres:16.9-bookworm",
                        "id": "sha256:" + "c" * 64,
                        "repo_digests": [],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )


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

    payload["release"] = "0.16.2.1"
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
    compatibility_payload["deployment_artifacts"]["adapter"]["dependencies"] = [
        "langchain>=1.0,<2"
    ]
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
        name: samples[:3]
        for name, samples in benchmark_payload["sample_distribution"].items()
    }
    benchmark.write_text(json.dumps(benchmark_payload), encoding="utf-8")
    result = _validate(manifest, evidence_root, "source")
    assert result.returncode != 0
    assert "performance evidence schema" in result.stderr


def test_final_release_evidence_requires_real_junit_spdx_and_sigstore(tmp_path: Path) -> None:
    evidence_root, manifest = _source_tree(tmp_path)
    _write_generated_evidence(evidence_root)
    assert _validate(manifest, evidence_root, "final").returncode == 0

    junit = evidence_root / "release/langgraph/junit.xml"
    junit.write_text('<testsuite tests="1" failures="1" errors="0"/>', encoding="utf-8")
    result = _validate(manifest, evidence_root, "final")
    assert result.returncode != 0
    assert "JUnit failures or errors" in result.stderr

    junit.write_text("this is a test source file, not JUnit", encoding="utf-8")
    result = _validate(manifest, evidence_root, "final")
    assert result.returncode != 0
    assert "JUnit XML" in result.stderr

    _write_generated_evidence(evidence_root)
    (evidence_root / "release/langgraph/image.spdx.json").write_text("{}", encoding="utf-8")
    result = _validate(manifest, evidence_root, "final")
    assert result.returncode != 0
    assert "SPDX JSON" in result.stderr

    _write_generated_evidence(evidence_root)
    (evidence_root / "release/langgraph/provenance.bundle.json").write_text(
        "{}", encoding="utf-8"
    )
    result = _validate(manifest, evidence_root, "final")
    assert result.returncode != 0
    assert "Sigstore bundle" in result.stderr
