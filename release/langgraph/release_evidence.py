"""Fail-closed validation for committed and generated release evidence."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from langgraph_benchmark import (
    CURRENT_RELEASE,
    DISTRIBUTION_NAMES,
    PREVIOUS_RELEASE,
    THRESHOLD_RULES,
    THRESHOLDS,
    TOKENS,
    distribution_statistics,
    evaluate,
)

ROOT = Path(__file__).resolve().parents[2]
REQUIRED_EVIDENCE = {
    "compatibility": {
        "kind": "compatibility-matrix",
        "artifacts": ["release/langgraph/compatibility.json"],
    },
    "performance": {
        "kind": "benchmark",
        "artifacts": [
            "release/langgraph/benchmark-baseline-0.16.1.7.json",
            "release/langgraph/benchmark-evidence.json",
        ],
    },
    "security": {
        "kind": "container-security",
        "artifacts": [
            "release/langgraph/image.spdx.json",
            "release/langgraph/provenance.bundle.json",
            "release/langgraph/image-compatibility.json",
        ],
    },
    "tests": {
        "kind": "junit",
        "artifacts": ["release/langgraph/junit.xml"],
    },
}
EXPECTED_DEPLOYMENT_ARTIFACTS = {
    "adapter": {
        "artifact": "zeroth-core[langgraph]",
        "dependencies": [
            "langchain>=1.0,<2",
            "langgraph>=1.0,<2",
            "langgraph-checkpoint-sqlite>=3.0,<4",
        ],
    },
    "gateway": {
        "artifact": "zeroth-core[langgraph-gateway]",
        "dependencies": ["httpx[http2]>=0.27", "websockets>=15,<16"],
    },
}
EXPECTED_RESOLVED = {
    "agent_server": "0.11.1",
    "langchain": "1.3.14",
    "langgraph": "1.2.9",
    "langgraph_checkpoint_sqlite": "3.1.1",
    "langgraph_sdk": "0.4.2",
    "python_image": "python:3.12.13-slim-bookworm",
    "postgres_image": "postgres:16.9-bookworm",
}
HARDWARE_KEYS = {"system", "release", "machine", "processor", "cpu_count", "python"}
BASELINE_SOURCE = {
    "commit": "d4f235f70f43f669d7d14df14a69b0cda10eaea5",
    "package_version": PREVIOUS_RELEASE,
    "path": "archived src/zeroth/integrations/langgraph",
}
BENCHMARK_KEYS = {
    "schema_version",
    "release",
    "methodology",
    "workload",
    "sample_count",
    "sample_distribution",
    "summary",
    "variance",
    "stream_ordering",
    "hardware",
    "baseline",
    "thresholds",
    "observed",
    "evaluation",
    "passed",
    "injected_regression",
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


def _positive_metrics(value: Any) -> bool:
    return isinstance(value, dict) and bool(value) and all(
        isinstance(item, (int, float))
        and not isinstance(item, bool)
        and math.isfinite(item)
        and item > 0
        for item in value.values()
    )


def _valid_hardware(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == HARDWARE_KEYS
        and all(item is not None and item != "" for item in value.values())
    )


def _validate_compatibility(path: Path, errors: list[str]) -> None:
    value = _json_file(path, "compatibility evidence", errors)
    expected_keys = {
        "schema_version",
        "release",
        "adapter_version",
        "tested",
        "deployment_artifacts",
        "resolved",
    }
    if value is None or (
        set(value) != expected_keys
        or value.get("schema_version") != 1
        or value.get("release") != CURRENT_RELEASE
        or value.get("adapter_version") != "1.0"
        or value.get("tested")
        != {"agent_server": "0.11.1", "langgraph": "1.2.9"}
        or value.get("deployment_artifacts") != EXPECTED_DEPLOYMENT_ARTIFACTS
        or value.get("resolved") != EXPECTED_RESOLVED
    ):
        errors.append("compatibility schema invalid")


def _validate_baseline(path: Path, errors: list[str]) -> dict[str, Any] | None:
    value = _json_file(path, "performance baseline", errors)
    expected_keys = {
        "schema_version",
        "release",
        "methodology",
        "sample_count",
        "metrics",
        "hardware",
        "source",
    }
    if value is None or (
        set(value) != expected_keys
        or value.get("schema_version") != 1
        or value.get("release") != PREVIOUS_RELEASE
        or value.get("methodology") != "langgraph-local-vs-loopback-sidecar"
        or not isinstance(value.get("sample_count"), int)
        or value["sample_count"] < 20
        or set(value.get("metrics", {})) != set(THRESHOLD_RULES)
        or not _positive_metrics(value.get("metrics"))
        or not _valid_hardware(value.get("hardware"))
        or value.get("source") != BASELINE_SOURCE
    ):
        errors.append("performance baseline schema invalid")
        return None
    return value


def _valid_distributions(sample_count: Any, distributions: Any) -> bool:
    return (
        isinstance(sample_count, int)
        and not isinstance(sample_count, bool)
        and sample_count >= 20
        and isinstance(distributions, dict)
        and set(distributions) == DISTRIBUTION_NAMES
        and all(
            isinstance(samples, list)
            and len(samples) == sample_count
            and all(
                isinstance(item, (int, float))
                and not isinstance(item, bool)
                and math.isfinite(item)
                for item in samples
            )
            for samples in distributions.values()
        )
    )


def _valid_methodology(value: Any) -> bool:
    expected_keys = {
        "kind",
        "description",
        "clock",
        "model_latency_included",
        "external_network_included",
    }
    return (
        isinstance(value, dict)
        and set(value) == expected_keys
        and value.get("kind") == "langgraph-local-vs-loopback-sidecar"
        and bool(value.get("description"))
        and value.get("clock") == "time.perf_counter"
        and value.get("model_latency_included") is False
        and value.get("external_network_included") is False
    )


def _derived_values(
    sample_count: Any, distributions: Any
) -> tuple[dict[str, Any], dict[str, float], dict[str, float]]:
    if not _valid_distributions(sample_count, distributions):
        return {}, {}, {}
    return distribution_statistics(distributions)


def _recomputed_evaluation(value: dict[str, Any]) -> dict[str, bool]:
    observed = value.get("observed")
    if (
        value.get("thresholds") != THRESHOLDS
        or not _positive_metrics(observed)
        or set(observed) != set(THRESHOLD_RULES)
    ):
        return {}
    return evaluate(observed)


def _benchmark_schema_valid(value: dict[str, Any], baseline: Any) -> bool:
    sample_count = value.get("sample_count")
    distributions = value.get("sample_distribution")
    summary, variance, observed = _derived_values(sample_count, distributions)
    recomputed = _recomputed_evaluation(value)
    workload = {
        "tokens_per_sample": len(TOKENS),
        "local_path": "govern_tools+StateGraph",
        "sidecar_path": "govern_tools+HttpToolDecisionClient",
    }
    return (
        set(value) == BENCHMARK_KEYS
        and value.get("schema_version") == 2
        and value.get("release") == CURRENT_RELEASE
        and _valid_methodology(value.get("methodology"))
        and value.get("workload") == workload
        and bool(summary)
        and value.get("summary") == summary
        and value.get("variance") == variance
        and value.get("observed") == observed
        and value.get("stream_ordering")
        == {"valid": True, "samples_checked": sample_count}
        and value.get("baseline") == baseline
        and value.get("thresholds") == THRESHOLDS
        and value.get("evaluation") == recomputed
        and set(recomputed) == set(THRESHOLD_RULES)
        and _valid_hardware(value.get("hardware"))
    )


def _validate_benchmark(
    path: Path, baseline: dict[str, Any] | None, errors: list[str]
) -> None:
    value = _json_file(path, "performance evidence", errors)
    if value is None:
        return
    if value.get("release") != CURRENT_RELEASE:
        errors.append(f"performance release must be {CURRENT_RELEASE}")
    recomputed = _recomputed_evaluation(value)
    if not _benchmark_schema_valid(value, baseline):
        errors.append("performance evidence schema invalid")
    if (
        value.get("passed") is not True
        or value.get("injected_regression") is not False
        or not recomputed
        or not all(recomputed.values())
    ):
        errors.append("performance evidence did not pass")


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
        errors_count = sum(int(suite.get("errors", "0")) for suite in suites)
    except ValueError:
        errors.append("test evidence is not JUnit XML")
        return
    if tests <= 0:
        errors.append("JUnit contains no test results")
    if failures or errors_count:
        errors.append("JUnit failures or errors are nonzero")


def _validate_spdx(path: Path, errors: list[str]) -> None:
    value = _json_file(path, "SPDX JSON", errors)
    if value is None or (
        not str(value.get("spdxVersion", "")).startswith("SPDX-")
        or not value.get("name")
        or not value.get("documentNamespace")
        or not isinstance(value.get("packages"), list)
        or not value["packages"]
    ):
        errors.append("security evidence is not valid nonempty SPDX JSON")


def _validate_sigstore(path: Path, errors: list[str]) -> None:
    value = _json_file(path, "Sigstore bundle", errors)
    if value is None or (
        not str(value.get("mediaType", "")).startswith(
            "application/vnd.dev.sigstore.bundle"
        )
        or not isinstance(value.get("verificationMaterial"), dict)
        or not value["verificationMaterial"]
        or not isinstance(value.get("dsseEnvelope"), dict)
        or not value["dsseEnvelope"].get("signatures")
    ):
        errors.append("provenance evidence is not a nonempty Sigstore bundle")


def _valid_image_entries(images: Any) -> bool:
    if not isinstance(images, list) or len(images) != 3:
        return False
    references = [str(image.get("reference")) for image in images if isinstance(image, dict)]
    application = [item for item in references if item.startswith("zeroth-core:")]
    expected_bases = {"python:3.12.13-slim-bookworm", "postgres:16.9-bookworm"}
    return (
        len(references) == 3
        and len(set(references)) == 3
        and expected_bases.issubset(references)
        and len(application) == 1
        and application[0].removeprefix("zeroth-core:").removeprefix("v")
        == CURRENT_RELEASE
        and all(
            set(image) == {"reference", "id", "repo_digests"}
            and re.fullmatch(r"sha256:[0-9a-f]{64}", str(image.get("id"))) is not None
            and isinstance(image.get("repo_digests"), list)
            for image in images
        )
    )


def _validate_images(path: Path, errors: list[str]) -> None:
    value = _json_file(path, "image compatibility evidence", errors)
    if value is None or (
        set(value) != {"schema_version", "release", "images"}
        or value.get("schema_version") != 1
        or value.get("release") != CURRENT_RELEASE
        or not _valid_image_entries(value.get("images"))
    ):
        errors.append("image compatibility evidence is invalid")


def validate_manifest(
    path: Path, *, phase: str = "source", evidence_root: Path = ROOT
) -> list[str]:
    """Return fail-closed source or generated release-evidence errors."""
    errors: list[str] = []
    manifest = _json_file(path, "manifest", errors)
    if manifest is None:
        return errors
    if (
        set(manifest) != {"schema_version", "release", "baseline_release", "evidence"}
        or manifest.get("schema_version") != 2
        or manifest.get("release") != CURRENT_RELEASE
        or manifest.get("baseline_release") != PREVIOUS_RELEASE
        or manifest.get("evidence") != REQUIRED_EVIDENCE
    ):
        return ["manifest schema or release is invalid"]
    performance = REQUIRED_EVIDENCE["performance"]["artifacts"]
    _validate_compatibility(evidence_root / "release/langgraph/compatibility.json", errors)
    baseline = _validate_baseline(evidence_root / performance[0], errors)
    _validate_benchmark(evidence_root / performance[1], baseline, errors)
    if phase == "final":
        security = REQUIRED_EVIDENCE["security"]["artifacts"]
        _validate_spdx(evidence_root / security[0], errors)
        _validate_sigstore(evidence_root / security[1], errors)
        _validate_images(evidence_root / security[2], errors)
        _validate_junit(evidence_root / "release/langgraph/junit.xml", errors)
    return errors
