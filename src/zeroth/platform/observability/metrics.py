"""Lightweight in-process metrics collector.

Tracks counters, histograms, and gauges and renders them in Prometheus text
format.  No external dependencies — uses Python's threading module for safety.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any


def _label_str(labels: dict[str, str]) -> str:
    if not labels:
        return ""
    pairs = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    return "{" + pairs + "}"


#: Recent samples retained per histogram series. The exported ``_count`` and
#: ``_sum`` come from running totals, so this window only bounds what a
#: future percentile reader would have to work with.
HISTOGRAM_SAMPLE_WINDOW = 1024


@dataclass
class MetricsCollector:
    """Thread-safe in-process metrics store."""

    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _counters: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _gauges: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    #: Bounded window of recent samples per series. The collector lives for the
    #: process, ``observe`` appended without any cap, eviction or reset, and
    #: ``snapshot`` copies rather than drains -- so a long-running worker grew
    #: this list for every run it executed.
    _histograms: dict[str, deque[float]] = field(default_factory=dict, init=False, repr=False)
    #: Running ``(count, sum)`` per series, kept separately so bounding the
    #: samples above does not make the exported ``_count`` go backwards. Those
    #: two are all ``render_prometheus_text`` ever needed from the full list.
    _histogram_totals: dict[str, tuple[int, float]] = field(
        default_factory=dict, init=False, repr=False
    )

    def increment(
        self, name: str, labels: dict[str, str] | None = None, amount: float = 1.0
    ) -> None:
        """Increment a counter."""
        key = name + _label_str(labels or {})
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + amount

    def gauge_set(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        """Set a gauge to an absolute value."""
        key = name + _label_str(labels or {})
        with self._lock:
            self._gauges[key] = value

    def observe(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        """Record a histogram observation."""
        key = name + _label_str(labels or {})
        with self._lock:
            samples = self._histograms.get(key)
            if samples is None:
                samples = deque(maxlen=HISTOGRAM_SAMPLE_WINDOW)
                self._histograms[key] = samples
            samples.append(value)
            count, total = self._histogram_totals.get(key, (0, 0.0))
            self._histogram_totals[key] = (count + 1, total + value)

    def snapshot(self) -> dict[str, Any]:
        """Return a copy of all current metric values.

        ``histograms`` holds the most recent ``HISTOGRAM_SAMPLE_WINDOW`` samples
        per series, not every sample ever observed; ``histogram_totals`` carries
        the full ``(count, sum)``.
        """
        with self._lock:
            return {
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
                "histograms": {k: list(v) for k, v in self._histograms.items()},
                "histogram_totals": dict(self._histogram_totals),
            }

    def render_prometheus_text(self) -> str:
        """Render all metrics in Prometheus text exposition format."""
        lines: list[str] = []
        snap = self.snapshot()

        for key, value in sorted(snap["counters"].items()):
            base = key.split("{")[0]
            lines.append(f"# TYPE {base} counter")
            lines.append(f"{key} {value:g}")

        for key, value in sorted(snap["gauges"].items()):
            base = key.split("{")[0]
            lines.append(f"# TYPE {base} gauge")
            lines.append(f"{key} {value:g}")

        # From the running totals, not from the retained window: a Prometheus
        # ``_count`` is monotonic, and deriving it from a bounded sample list
        # would make it fall back as old samples aged out.
        for key, (count, total) in sorted(snap["histogram_totals"].items()):
            base = key.split("{")[0]
            lines.append(f"# TYPE {base} histogram")
            lines.append(f"{key}_count {count}")
            lines.append(f"{key}_sum {total:g}")

        return "\n".join(lines) + "\n" if lines else ""
