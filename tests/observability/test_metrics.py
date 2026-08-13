"""Tests for the MetricsCollector."""

from __future__ import annotations

import threading

import pytest

from zeroth.platform.observability.metrics import HISTOGRAM_SAMPLE_WINDOW, MetricsCollector


def test_counter_increments(metrics: MetricsCollector) -> None:
    metrics.increment("zeroth_runs_started_total")
    metrics.increment("zeroth_runs_started_total")
    text = metrics.render_prometheus_text()
    assert "zeroth_runs_started_total 2" in text


def test_histogram_records_observation(metrics: MetricsCollector) -> None:
    metrics.observe("zeroth_run_duration_seconds", 1.5)
    metrics.observe("zeroth_run_duration_seconds", 2.5)
    text = metrics.render_prometheus_text()
    assert "zeroth_run_duration_seconds_count 2" in text
    assert "zeroth_run_duration_seconds_sum" in text


def test_gauge_set_overrides_previous(metrics: MetricsCollector) -> None:
    metrics.gauge_set("zeroth_queue_depth", 10)
    metrics.gauge_set("zeroth_queue_depth", 5)
    text = metrics.render_prometheus_text()
    assert "zeroth_queue_depth 5" in text


def test_labels_are_included_in_output(metrics: MetricsCollector) -> None:
    metrics.increment("zeroth_policy_denials_total", {"policy": "safety"})
    text = metrics.render_prometheus_text()
    assert 'zeroth_policy_denials_total{policy="safety"} 1' in text


def test_thread_safe_increments(metrics: MetricsCollector) -> None:
    n = 100
    threads = [
        threading.Thread(target=lambda: metrics.increment("zeroth_runs_completed_total"))
        for _ in range(n)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    text = metrics.render_prometheus_text()
    assert f"zeroth_runs_completed_total {n}" in text


# --- ZER-48 / A08-3: a bounded window with monotonic totals -------------------
#
# ``observe`` appended to a plain list with no cap, eviction or reset, and the
# collector lives for the life of the process -- so a long-running worker grew
# one entry per observation forever. The window bounds *retention*; ``_count``
# and ``_sum`` come from running totals kept alongside it, so bounding the
# samples cannot make an exported counter go backwards. Both halves have to be
# asserted together: capping the samples without the separate totals silently
# breaks Prometheus, and keeping the totals without the cap is the leak.

#: Enough past the window to prove eviction rather than an off-by-one.
_OVERFLOW = 10


def _observe_a_full_window_and_then_some(metrics: MetricsCollector) -> None:
    """Record ``HISTOGRAM_SAMPLE_WINDOW + _OVERFLOW`` distinct observations."""
    for value in range(HISTOGRAM_SAMPLE_WINDOW + _OVERFLOW):
        metrics.observe("zeroth_run_duration_seconds", float(value))


def test_histogram_retains_only_the_sample_window(metrics: MetricsCollector) -> None:
    """Retention stops at the window however many observations arrive."""
    _observe_a_full_window_and_then_some(metrics)

    samples = metrics.snapshot()["histograms"]["zeroth_run_duration_seconds"]

    assert len(samples) == HISTOGRAM_SAMPLE_WINDOW


def test_histogram_evicts_the_oldest_samples_not_the_newest(metrics: MetricsCollector) -> None:
    """The retained window is the tail: the first ``_OVERFLOW`` values aged out."""
    _observe_a_full_window_and_then_some(metrics)

    samples = metrics.snapshot()["histograms"]["zeroth_run_duration_seconds"]

    assert samples[0] == float(_OVERFLOW)
    assert samples[-1] == float(HISTOGRAM_SAMPLE_WINDOW + _OVERFLOW - 1)


def test_exported_count_and_sum_cover_every_observation(metrics: MetricsCollector) -> None:
    """``_count``/``_sum`` stay monotonic over all observations, not over the window.

    Derived from the running totals rather than from the retained list: a
    ``_count`` computed off a bounded window would fall back as samples age out,
    and a Prometheus counter that decreases is read as a process restart.
    """
    observations = HISTOGRAM_SAMPLE_WINDOW + _OVERFLOW
    _observe_a_full_window_and_then_some(metrics)

    text = metrics.render_prometheus_text()

    assert f"zeroth_run_duration_seconds_count {observations}" in text
    expected_sum = observations * (observations - 1) // 2
    assert f"zeroth_run_duration_seconds_sum {expected_sum}" in text


def test_the_window_is_per_series(metrics: MetricsCollector) -> None:
    """One busy series does not evict another series' samples."""
    _observe_a_full_window_and_then_some(metrics)
    metrics.observe("zeroth_node_duration_seconds", 1.5)

    histograms = metrics.snapshot()["histograms"]

    assert histograms["zeroth_node_duration_seconds"] == [1.5]
    assert len(histograms["zeroth_run_duration_seconds"]) == HISTOGRAM_SAMPLE_WINDOW


@pytest.fixture
def metrics() -> MetricsCollector:
    return MetricsCollector()
