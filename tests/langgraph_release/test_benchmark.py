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
    assert report["methodology"]["kind"] == "synthetic-local-vs-sidecar"
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
    assert report["baseline"] and report["thresholds"]

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
