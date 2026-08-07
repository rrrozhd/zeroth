#!/usr/bin/env python3
"""Reproducible LangGraph release benchmark, smoke check, and evidence gate."""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import time
import tracemalloc
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
REQUIRED_EVIDENCE = frozenset({"compatibility", "security", "performance", "tests"})
BASELINE = {
    "release": "0.16.2",
    "metrics": {
        "sidecar_overhead_p95_ms": 5.0,
        "ttft_p95_ms": 2.0,
        "decision_latency_p95_ms": 2.0,
        "throughput_tokens_per_second": 5_000.0,
        "peak_memory_bytes": 100_000,
    },
}
THRESHOLDS = {
    "sidecar_overhead_p95_ms": {"maximum": 50.0},
    "ttft_p95_ms": {"maximum": 20.0},
    "decision_latency_p95_ms": {"maximum": 20.0},
    "throughput_tokens_per_second": {"minimum": 500.0},
    "peak_memory_bytes": {"maximum": 2_000_000},
}


def _percentile(values: list[float], fraction: float = 0.95) -> float:
    return sorted(values)[min(len(values) - 1, round((len(values) - 1) * fraction))]


def _sample(injected_delay: float) -> tuple[dict[str, float], list[int]]:
    tokens = tuple(f"token-{index}" for index in range(32))
    request = {"thread_id": "benchmark-thread", "input": "release probe"}

    start = time.perf_counter()
    local = tuple(tokens)
    local_ms = (time.perf_counter() - start) * 1000

    tracemalloc.start()
    start = time.perf_counter()
    wire = json.dumps({"request": request, "tokens": tokens}, separators=(",", ":"))
    if injected_delay:
        time.sleep(injected_delay)
    sidecar = json.loads(wire)
    sidecar_ms = (time.perf_counter() - start) * 1000
    _, peak_memory = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    stream = iter(enumerate(sidecar["tokens"], start=1))
    start = time.perf_counter()
    first = next(stream)
    first_at = time.perf_counter()
    order = [first[0]]
    cadence: list[float] = []
    previous = first_at
    for sequence, _token in stream:
        now = time.perf_counter()
        cadence.append((now - previous) * 1000)
        previous = now
        order.append(sequence)
    elapsed = max(time.perf_counter() - start, 1e-9)

    decision_start = time.perf_counter()
    decision = json.loads(json.dumps({"decision": "allow", "request": request}))
    assert decision["decision"] == "allow" and local == tokens
    decision_ms = (time.perf_counter() - decision_start) * 1000
    return (
        {
            "local_overhead_ms": local_ms,
            "sidecar_overhead_ms": max(0.0, sidecar_ms - local_ms),
            "ttft_ms": (first_at - start) * 1000,
            "token_cadence_ms": statistics.fmean(cadence),
            "decision_latency_ms": decision_ms,
            "throughput_tokens_per_second": len(tokens) / elapsed,
            "peak_memory_bytes": float(peak_memory),
        },
        order,
    )


def benchmark(samples: int, *, inject_regression: bool = False) -> dict[str, Any]:
    """Measure the same deterministic in-process and serialized gateway work repeatedly."""
    if samples < 3:
        raise ValueError("benchmark needs at least three samples")
    rows, orders = zip(
        *(_sample(0.1 if inject_regression else 0.0) for _ in range(samples)), strict=True
    )
    distributions = {name: [round(row[name], 6) for row in rows] for name in rows[0]}
    summary = {
        name: {
            "mean": round(statistics.fmean(values), 6),
            "p95": round(_percentile(values), 6),
        }
        for name, values in distributions.items()
    }
    observed = {
        "sidecar_overhead_p95_ms": summary["sidecar_overhead_ms"]["p95"],
        "ttft_p95_ms": summary["ttft_ms"]["p95"],
        "decision_latency_p95_ms": summary["decision_latency_ms"]["p95"],
        "throughput_tokens_per_second": summary["throughput_tokens_per_second"]["mean"],
        "peak_memory_bytes": max(distributions["peak_memory_bytes"]),
    }
    evaluation = {
        name: value <= rule["maximum"]
        if "maximum" in rule
        else value >= rule["minimum"]
        for name, (value, rule) in (
            (name, (observed[name], rule)) for name, rule in THRESHOLDS.items()
        )
    }
    return {
        "schema_version": 1,
        "methodology": {
            "kind": "synthetic-local-vs-sidecar",
            "description": (
                "Repeated local tuple traversal versus the same request serialized through "
                "an in-process JSON sidecar boundary; no network or model latency is claimed."
            ),
            "clock": "time.perf_counter",
        },
        "sample_count": samples,
        "sample_distribution": distributions,
        "summary": summary,
        "variance": {
            name: round(statistics.pvariance(values), 6)
            for name, values in distributions.items()
        },
        "stream_ordering": {
            "valid": all(order == list(range(1, 33)) for order in orders),
            "samples_checked": samples,
        },
        "hardware": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "processor": platform.processor() or "unknown",
            "cpu_count": os.cpu_count(),
            "python": platform.python_version(),
        },
        "baseline": BASELINE,
        "thresholds": THRESHOLDS,
        "observed": observed,
        "evaluation": evaluation,
        "passed": all(evaluation.values()),
        "injected_regression": inject_regression,
    }


def validate_manifest(path: Path) -> list[str]:
    """Return fail-closed release-evidence errors."""
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return [f"manifest unreadable: {error}"]
    evidence = manifest.get("evidence")
    if not isinstance(evidence, dict):
        return ["missing evidence object"]
    errors = [f"missing {name} evidence" for name in sorted(REQUIRED_EVIDENCE - evidence.keys())]
    for name in sorted(REQUIRED_EVIDENCE & evidence.keys()):
        entry = evidence[name]
        if not isinstance(entry, dict) or entry.get("status") != "passed":
            errors.append(f"{name} evidence is not passed")
            continue
        artifacts = entry.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            errors.append(f"{name} evidence has no artifacts")
            continue
        for artifact in artifacts:
            if not isinstance(artifact, str) or not (ROOT / artifact).is_file():
                errors.append(f"{name} evidence artifact is missing: {artifact}")
    return errors


def smoke(url: str) -> None:
    """Fail unless dependency-aware readiness reports a service that can receive traffic."""
    with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - operator URL
        payload = json.load(response)
    if payload.get("status") not in {"ok", "degraded"} or not payload.get("checks"):
        raise RuntimeError(f"readiness failed: {payload!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    bench = commands.add_parser("benchmark")
    bench.add_argument("--samples", type=int, default=20)
    bench.add_argument("--inject-regression", action="store_true")
    bench.add_argument("--output", type=Path, required=True)
    check = commands.add_parser("validate")
    check.add_argument("--manifest", type=Path, required=True)
    probe = commands.add_parser("smoke")
    probe.add_argument("--url", default="http://127.0.0.1:8000/health/ready")
    args = parser.parse_args(argv)

    if args.command == "benchmark":
        report = benchmark(args.samples, inject_regression=args.inject_regression)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        if not report["passed"]:
            print("threshold failed", file=sys.stderr)
            return 1
        return 0
    if args.command == "validate":
        errors = validate_manifest(args.manifest)
        if errors:
            print("\n".join(errors), file=sys.stderr)
            return 1
        print("release evidence complete")
        return 0
    try:
        smoke(args.url)
    except Exception as error:  # noqa: BLE001 - CLI must return a simple failure
        print(f"smoke failed: {error}", file=sys.stderr)
        return 1
    print("readiness smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
