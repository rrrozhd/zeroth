"""Tests for the MetricsCollector."""

from __future__ import annotations

import threading

import pytest

from zeroth.core.observability.metrics import MetricsCollector


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


@pytest.fixture
def metrics() -> MetricsCollector:
    return MetricsCollector()
