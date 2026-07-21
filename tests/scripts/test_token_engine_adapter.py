from __future__ import annotations

import inspect

import pytest

from scripts.token_engine_checker.adapter import ProductionAdapter, UnsupportedValidCaseError
from scripts.token_engine_checker.explorer import compare_case, schedule_orders
from scripts.token_engine_checker.generator import enumerate_cases, generate_topologies
from scripts.token_engine_checker.models import Case
from scripts.token_engine_checker.normalization import normalize_trace
from scripts.token_engine_checker.oracle import Oracle


def _case() -> Case:
    topology = next(topology for topology in generate_topologies(4) if len(topology.edges) == 4)
    return next(
        case
        for case in enumerate_cases(topology)
        if all(case.enabled)
        and case.state.payload_json == '{"p":1}'
        and case.state.reducer == "collect"
        and case.state.retry == "fail-first"
        and case.state.checkpoint == "after-claim"
        and case.state.cancellation == "none"
    )


def test_only_adapter_imports_production_runtime() -> None:
    source = inspect.getsource(inspect.getmodule(ProductionAdapter))

    assert "zeroth.runtime" in source


def test_production_pure_transitions_match_independent_oracle() -> None:
    case = _case()

    expected = normalize_trace(Oracle().run(case))
    observed = normalize_trace(ProductionAdapter().run(case))

    assert observed == expected


def test_ready_width_six_is_schedule_exhaustive() -> None:
    orders = schedule_orders(tuple("abcdef"), seed=9, case_digest="case")

    assert len(orders) == 720
    assert len(set(orders)) == 720


def test_wide_ready_set_uses_canonical_reverse_and_seeded_orders() -> None:
    ready = tuple("abcdefg")

    orders = schedule_orders(ready, seed=120600, case_digest="case")

    assert orders[0] == ready
    assert orders[1] == tuple(reversed(ready))
    assert orders == schedule_orders(ready, seed=120600, case_digest="case")
    assert len(orders) == 3


def test_valid_adapter_rejection_is_a_failure_not_a_filter(monkeypatch) -> None:
    def reject(_self, _case, schedule=None):
        raise UnsupportedValidCaseError("shape")

    monkeypatch.setattr(ProductionAdapter, "run", reject)

    comparison = compare_case(_case(), seed=1)

    assert not comparison.passed
    assert comparison.failure_kind == "unsupported_valid_case"


def test_trace_normalization_does_not_hide_missing_resolution() -> None:
    case = _case()
    trace = Oracle().run(case)

    assert normalize_trace(trace) != normalize_trace(trace.with_resolutions(trace.resolutions[1:]))
