"""Real local-versus-sidecar LangGraph release benchmark."""

from __future__ import annotations

import json
import os
import platform
import statistics
import threading
import time
import tracemalloc
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, TypedDict

ROOT = Path(__file__).resolve().parents[2]
CURRENT_RELEASE = "0.23.3"
PREVIOUS_RELEASE = "0.16.1.7"
BASELINE_PATH = ROOT / "release/langgraph/benchmark-baseline-0.16.1.7.json"
BASELINE = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
BASELINE_METRICS = BASELINE["metrics"]

#: The exact bytes the thresholds below were derived from.
#:
#: The regression gate compares a measured run against ``THRESHOLD_RULES``. While
#: those rules were *computed* from ``BASELINE_METRICS`` at import time, the gate's
#: expected value came from the same committed file it was meant to police:
#: scaling every metric by ten scaled every threshold by ten, and the run still
#: passed. That is not a regression gate, it is a tautology.
#:
#: So the numbers are literals now, and the file they came from is pinned. Editing
#: the baseline can no longer move a threshold, and the edit is detected rather
#: than silently absorbed. Regenerating the baseline for a new release is a
#: deliberate act: re-measure, re-derive, and update both this digest and the
#: literals together.
BASELINE_DIGEST = "sha256:763b79d5f291f8412e0491d7605b59077bf157b0d3f9b55d532b950f5111be6d"

#: Multiplier and floor each literal was derived with, kept so the derivation
#: stays machine-checked rather than described in prose. See
#: ``tests/langgraph_release/test_benchmark.py``.
THRESHOLD_DERIVATION = {
    "sidecar_overhead_p95_ms": ("maximum", 15.0, 4.0),
    "ttft_p95_ms": ("maximum", 15.0, 4.0),
    "decision_latency_p95_ms": ("maximum", 10.0, 4.0),
    "throughput_tokens_per_second": ("minimum", None, 0.25),
    "peak_memory_bytes": ("maximum", 2_000_000, 3.0),
}

THRESHOLD_RULES = {
    "sidecar_overhead_p95_ms": {"maximum": 15.0},
    "ttft_p95_ms": {"maximum": 15.5795},
    "decision_latency_p95_ms": {"maximum": 10.0},
    "throughput_tokens_per_second": {"minimum": 2136.66121225},
    "peak_memory_bytes": {"maximum": 2_000_000},
}
THRESHOLDS = {"derived_from": PREVIOUS_RELEASE, "rules": THRESHOLD_RULES}
TOKENS = tuple(f"token-{index}" for index in range(32))
DISTRIBUTION_NAMES = {
    "local_overhead_ms",
    "sidecar_overhead_ms",
    "ttft_ms",
    "token_cadence_ms",
    "decision_latency_ms",
    "throughput_tokens_per_second",
    "peak_memory_bytes",
}


class _BenchmarkState(TypedDict):
    tokens: tuple[str, ...]


class _AllowDecisionClient:
    def decide(self, _action: Any, _context: Any) -> Any:
        from zeroth.integrations.langgraph import ToolDecision, ToolDecisionKind

        return ToolDecision(ToolDecisionKind.ALLOW, "unknown_error")


class _TimedDecisionClient:
    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate
        self.last_ms = 0.0

    def decide(self, action: Any, context: Any) -> Any:
        started = time.perf_counter()
        try:
            return self.delegate.decide(action, context)
        finally:
            self.last_ms = (time.perf_counter() - started) * 1000


class _DecisionHandler(BaseHTTPRequestHandler):
    server: Any

    def do_POST(self) -> None:  # noqa: N802 - stdlib HTTP callback name
        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length))
        if self.path != "/v1/enforcement/decisions" or body.get("schema_version") != 1:
            self.send_error(400)
            return
        if self.server.injected_delay:
            time.sleep(self.server.injected_delay)
        payload = json.dumps(
            {
                "schema_version": 1,
                "decision_id": "benchmark-decision",
                "kind": "allow",
                "reason_code": "unknown_error",
                "approval_ref": None,
                "policy_version": "benchmark-policy",
                "tenant_id": "benchmark-tenant",
                "issued_at": datetime.now(UTC).isoformat(),
            },
            separators=(",", ":"),
        ).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def _start_decision_sidecar(
    injected_delay: float,
) -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DecisionHandler)
    server.injected_delay = injected_delay  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _graph(client: Any) -> Any:
    from langgraph.config import get_stream_writer
    from langgraph.graph import END, START, StateGraph

    from zeroth.integrations.langgraph import (
        SideEffectClass,
        ToolGovernanceContext,
        govern_tools,
    )

    def deterministic_workload() -> tuple[str, ...]:
        writer = get_stream_writer()
        for sequence, token in enumerate(TOKENS, start=1):
            writer({"sequence": sequence, "token": token})
        return TOKENS

    [governed] = govern_tools(
        [deterministic_workload],
        context=ToolGovernanceContext(
            tenant_id="benchmark-tenant",
            principal_id="benchmark-principal",
            run_id="benchmark-run",
            thread_id="benchmark-thread",
            correlation_id="benchmark-correlation",
        ),
        client=client,
        side_effect=lambda _tool: SideEffectClass.READ_ONLY,
    )

    def run(_state: _BenchmarkState) -> _BenchmarkState:
        return {"tokens": governed()}

    builder = StateGraph(_BenchmarkState)
    builder.add_node("workload", run)
    builder.add_edge(START, "workload")
    builder.add_edge("workload", END)
    return builder.compile()


def percentile(values: list[float], fraction: float = 0.95) -> float:
    """Return the nearest-rank value used by generation and validation."""
    return sorted(values)[min(len(values) - 1, round((len(values) - 1) * fraction))]


def _run_stream(graph: Any, sample: int) -> tuple[float, float, float, list[int]]:
    started = time.perf_counter()
    first_at: float | None = None
    previous: float | None = None
    order: list[int] = []
    cadence: list[float] = []
    for chunk in graph.stream(
        {"tokens": ()},
        {"configurable": {"thread_id": f"benchmark-{sample}"}},
        stream_mode="custom",
    ):
        now = time.perf_counter()
        if first_at is None:
            first_at = now
        elif previous is not None:
            cadence.append((now - previous) * 1000)
        previous = now
        order.append(chunk["sequence"])
    ended = time.perf_counter()
    assert first_at is not None
    return (
        (ended - started) * 1000,
        (first_at - started) * 1000,
        statistics.fmean(cadence),
        order,
    )


def _sample(
    local_graph: Any,
    sidecar_graph: Any,
    timer: _TimedDecisionClient,
    sample: int,
) -> tuple[dict[str, float], list[int]]:
    local_ms, _local_ttft, _local_cadence, local_order = _run_stream(local_graph, sample)
    tracemalloc.start()
    sidecar_ms, ttft_ms, cadence_ms, sidecar_order = _run_stream(sidecar_graph, sample)
    _, peak_memory = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert local_order == sidecar_order
    elapsed = max(sidecar_ms / 1000, 1e-9)
    return (
        {
            "local_overhead_ms": local_ms,
            "sidecar_overhead_ms": max(0.0, sidecar_ms - local_ms),
            "ttft_ms": ttft_ms,
            "token_cadence_ms": cadence_ms,
            "decision_latency_ms": timer.last_ms,
            "throughput_tokens_per_second": len(TOKENS) / elapsed,
            "peak_memory_bytes": float(peak_memory),
        },
        sidecar_order,
    )


def _measure(samples: int, injected_delay: float) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    from zeroth.integrations.langgraph import HttpToolDecisionClient

    local_graph = _graph(_AllowDecisionClient())
    server, thread = _start_decision_sidecar(injected_delay)
    sidecar = HttpToolDecisionClient(
        base_url=f"http://127.0.0.1:{server.server_port}",
        deployment_ref="benchmark-deployment",
        api_key="benchmark-key",
    )
    timer = _TimedDecisionClient(sidecar)
    sidecar_graph = _graph(timer)
    try:
        return tuple(
            zip(
                *(_sample(local_graph, sidecar_graph, timer, index) for index in range(samples)),
                strict=True,
            )
        )
    finally:
        sidecar.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def distribution_statistics(
    distributions: dict[str, list[float]],
) -> tuple[dict[str, dict[str, float]], dict[str, float], dict[str, float]]:
    """Derive summaries, variance, and release metrics from raw samples."""
    summary = {
        name: {
            "mean": round(statistics.fmean(values), 6),
            "p95": round(percentile(values), 6),
        }
        for name, values in distributions.items()
    }
    variance = {
        name: round(statistics.pvariance(values), 6) for name, values in distributions.items()
    }
    observed = {
        "sidecar_overhead_p95_ms": summary["sidecar_overhead_ms"]["p95"],
        "ttft_p95_ms": summary["ttft_ms"]["p95"],
        "decision_latency_p95_ms": summary["decision_latency_ms"]["p95"],
        "throughput_tokens_per_second": summary["throughput_tokens_per_second"]["mean"],
        "peak_memory_bytes": max(distributions["peak_memory_bytes"]),
    }
    return summary, variance, observed


def evaluate(observed: dict[str, float]) -> dict[str, bool]:
    """Evaluate fixed, baseline-derived release thresholds."""
    return {
        name: observed[name] <= rule["maximum"]
        if "maximum" in rule
        else observed[name] >= rule["minimum"]
        for name, rule in THRESHOLD_RULES.items()
    }


def _hardware() -> dict[str, Any]:
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "processor": platform.processor() or "unknown",
        "cpu_count": os.cpu_count(),
        "python": platform.python_version(),
    }


def benchmark(samples: int, *, inject_regression: bool = False) -> dict[str, Any]:
    """Measure one real LangGraph workload through local and loopback decision paths."""
    if samples < 3:
        raise ValueError("benchmark needs at least three samples")
    rows, orders = _measure(samples, 0.1 if inject_regression else 0.0)
    distributions = {name: [round(row[name], 6) for row in rows] for name in rows[0]}
    summary, variance, observed = distribution_statistics(distributions)
    evaluation = evaluate(observed)
    return {
        "schema_version": 2,
        "release": CURRENT_RELEASE,
        "methodology": {
            "kind": "langgraph-local-vs-loopback-sidecar",
            "description": (
                "Repeated public govern_tools StateGraph execution versus the same graph "
                "using HttpToolDecisionClient over a real loopback HTTP sidecar."
            ),
            "clock": "time.perf_counter",
            "model_latency_included": False,
            "external_network_included": False,
        },
        "workload": {
            "tokens_per_sample": len(TOKENS),
            "local_path": "govern_tools+StateGraph",
            "sidecar_path": "govern_tools+HttpToolDecisionClient",
        },
        "sample_count": samples,
        "sample_distribution": distributions,
        "summary": summary,
        "variance": variance,
        "stream_ordering": {
            "valid": all(order == list(range(1, len(TOKENS) + 1)) for order in orders),
            "samples_checked": samples,
        },
        "hardware": _hardware(),
        "baseline": BASELINE,
        "thresholds": THRESHOLDS,
        "observed": observed,
        "evaluation": evaluation,
        "passed": all(evaluation.values()),
        "injected_regression": inject_regression,
    }
