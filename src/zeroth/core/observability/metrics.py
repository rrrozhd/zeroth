"""Lightweight in-process metrics collector.

Tracks counters, histograms, and gauges and renders them in Prometheus text
format.  No external dependencies — uses Python's threading module for safety.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any


def _label_str(labels: dict[str, str]) -> str:
    if not labels:
        return ""
    pairs = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    return "{" + pairs + "}"


@dataclass
class MetricsCollector:
    """Thread-safe in-process metrics store."""

    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _counters: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _gauges: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _histograms: dict[str, list[float]] = field(default_factory=dict, init=False, repr=False)

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
            self._histograms.setdefault(key, []).append(value)

    def snapshot(self) -> dict[str, Any]:
        """Return a copy of all current metric values."""
        with self._lock:
            return {
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
                "histograms": {k: list(v) for k, v in self._histograms.items()},
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

        for key, observations in sorted(snap["histograms"].items()):
            base = key.split("{")[0]
            lines.append(f"# TYPE {base} histogram")
            count = len(observations)
            total = sum(observations)
            lines.append(f"{key}_count {count}")
            lines.append(f"{key}_sum {total:g}")

        return "\n".join(lines) + "\n" if lines else ""
