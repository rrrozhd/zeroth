from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "release/langgraph/harness.py"


def test_benchmark_records_release_metrics_and_rejects_regression(tmp_path: Path) -> None:
    report_path = tmp_path / "benchmark.json"
    result = subprocess.run(
        [sys.executable, HARNESS, "benchmark", "--samples", "5", "--output", report_path],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["methodology"]["kind"] == "langgraph-local-vs-loopback-sidecar"
    assert report["methodology"]["model_latency_included"] is False
    assert report["methodology"]["external_network_included"] is False
    assert report["workload"]["local_path"] == "govern_tools+StateGraph"
    assert report["workload"]["sidecar_path"] == "govern_tools+HttpToolDecisionClient"
    assert report["sample_count"] == 5
    assert {
        "local_overhead_ms",
        "sidecar_overhead_ms",
        "ttft_ms",
        "token_cadence_ms",
        "decision_latency_ms",
        "throughput_tokens_per_second",
        "peak_memory_bytes",
    } <= report["sample_distribution"].keys()
    assert report["stream_ordering"]["valid"] is True
    assert report["hardware"]["python"]
    assert report["variance"]
    assert report["baseline"]["release"] == "0.16.1.7"
    assert report["baseline"]["sample_count"] >= 20
    assert report["baseline"]["source"] == {
        "commit": "d4f235f70f43f669d7d14df14a69b0cda10eaea5",
        "package_version": "0.16.1.7",
        "path": "archived src/zeroth/integrations/langgraph",
    }
    assert report["thresholds"]["derived_from"] == "0.16.1.7"

    regressed = subprocess.run(
        [
            sys.executable,
            HARNESS,
            "benchmark",
            "--samples",
            "3",
            "--inject-regression",
            "--output",
            tmp_path / "regressed.json",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert regressed.returncode != 0
    assert "threshold failed" in regressed.stderr


def _benchmark_module():
    """Import the benchmark module through its package path.

    No ``sys.path`` prepend: the bare sibling imports that forced one were
    qualified in ZER-41, so ``release.langgraph.*`` resolves the way any other
    consumer would import it.
    """
    import importlib

    return importlib.import_module("release.langgraph.langgraph_benchmark")


def test_threshold_independent_of_the_committed_baseline(tmp_path: Path) -> None:
    """Editing the baseline must not move a threshold.

    Measured before this fix: scaling the five committed baseline metrics by ten
    scaled every threshold by exactly ten -- sidecar 15.0 to 127.71, ttft 15.5795
    to 155.795, decision 10.0 to 83.83, throughput 2136.66 to 213.67, memory
    2,000,000 to 5,174,910 -- and the baseline validator returned an empty error
    list on the scaled file. A gate whose expected value comes from the artifact
    under test cannot fail.
    """
    import shutil

    # The module resolves its baseline relative to its own location, so importing
    # a copy from a tree whose baseline has been scaled is what actually tests
    # independence. Asserting the thresholds in *this* process would only restate
    # the literals, which is true either way.
    package = tmp_path / "release/langgraph"
    package.mkdir(parents=True)
    (tmp_path / "release/__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    shutil.copy2(ROOT / "release/langgraph/langgraph_benchmark.py", package)
    source = ROOT / "release/langgraph/benchmark-baseline-0.16.1.7.json"
    original = json.loads(source.read_text(encoding="utf-8"))
    scaled = dict(original)
    scaled["metrics"] = {name: value * 10 for name, value in original["metrics"].items()}
    (package / source.name).write_text(json.dumps(scaled, indent=2, sort_keys=True), "utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json, langgraph_benchmark as b; print(json.dumps(b.THRESHOLD_RULES))",
        ],
        cwd=package,
        check=False,
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(package)},
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "sidecar_overhead_p95_ms": {"maximum": 15.0},
        "ttft_p95_ms": {"maximum": 15.5795},
        "decision_latency_p95_ms": {"maximum": 10.0},
        "throughput_tokens_per_second": {"minimum": 2136.66121225},
        "peak_memory_bytes": {"maximum": 2_000_000},
    }


def test_threshold_literals_still_equal_their_documented_derivation() -> None:
    """The literals are frozen, but the derivation stays machine-checked.

    A comment recording ``max(15.0, baseline * 4)`` would be documentation. This
    recomputes it against the pinned baseline, so a legitimate re-measurement
    fails loudly here -- telling you to re-derive and re-pin together -- instead
    of silently moving the thresholds it is supposed to police.
    """
    benchmark = _benchmark_module()

    for name, (kind, floor, multiplier) in benchmark.THRESHOLD_DERIVATION.items():
        derived = benchmark.BASELINE_METRICS[name] * multiplier
        expected = derived if floor is None else max(floor, derived)
        assert benchmark.THRESHOLD_RULES[name] == {kind: expected}, name


def test_an_edited_baseline_is_detected_by_its_pinned_digest(tmp_path: Path) -> None:
    """A tampered baseline fails validation instead of being absorbed."""
    import importlib

    release_evidence = importlib.import_module("release.langgraph.release_evidence")
    source = ROOT / "release/langgraph/benchmark-baseline-0.16.1.7.json"

    honest_errors: list[str] = []
    assert release_evidence._validate_baseline(source, honest_errors) is not None
    assert honest_errors == []

    original = json.loads(source.read_text(encoding="utf-8"))
    scaled = dict(original)
    scaled["metrics"] = {name: value * 10 for name, value in original["metrics"].items()}
    tampered = tmp_path / "benchmark-baseline-0.16.1.7.json"
    tampered.write_text(json.dumps(scaled, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    errors: list[str] = []

    assert release_evidence._validate_baseline(tampered, errors) is None
    assert errors and "does not match the digest" in errors[0]
