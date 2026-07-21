from __future__ import annotations

import inspect
from itertools import permutations

import pytest

from scripts.token_engine_checker.adapter import ProductionAdapter, UnsupportedValidCaseError
from scripts.token_engine_checker.explorer import compare_case, schedule_orders, schedule_plans
from scripts.token_engine_checker.generator import enumerate_cases, generate_topologies
from scripts.token_engine_checker.models import Case, Edge, State, Topology
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


def _structured_case() -> Case:
    return Case(
        Topology(
            ("n0", "n1", "n2", "n3"),
            (
                Edge("e0", "n0", "n1", 0),
                Edge("e1", "n0", "n2", 0),
                Edge("e2", "n1", "n3", 0),
                Edge("e3", "n2", "n3", 0),
            ),
        ),
        (True, True, True, True),
        (),
        State('{"p":1}', "collect", "fail-first", "after-claim", "none"),
    )


def test_only_adapter_imports_production_runtime() -> None:
    source = inspect.getsource(inspect.getmodule(ProductionAdapter))

    assert "zeroth.runtime" in source


def test_production_pure_transitions_match_independent_oracle() -> None:
    case = _case()

    expected = normalize_trace(Oracle().run(case))
    observed = normalize_trace(ProductionAdapter().run(case))

    assert observed == expected


def test_production_adapter_exercises_structured_scopes_and_repository_cas() -> None:
    trace = ProductionAdapter().run(_structured_case())
    production = trace.persisted_state["production"]

    assert production["join"]["state"] == "closed"
    assert production["join"]["continuation_created"] is True
    assert production["loop"]["state"] == "not_applicable"
    assert production["lifecycle"]["checkpoint"] == "after-claim"
    assert production["repository"]["cas_writes"] > 0
    assert production["repository"]["reloads"] > 0


def test_production_adapter_exercises_actual_back_edge_loop_lifecycle() -> None:
    case = Case(
        Topology(
            ("n0", "n1", "n2", "n3"),
            (
                Edge("e0", "n0", "n1", 0),
                Edge("e1", "n1", "n0", 0),
                Edge("e2", "n1", "n2", 0),
                Edge("e3", "n2", "n3", 0),
            ),
        ),
        (True, True, True, True),
        (),
        State("null", "collect", "none", "none", "none"),
    )

    loop = ProductionAdapter().run(case).persisted_state["production"]["loop"]

    assert loop["state"] == "completed"
    assert loop["back_edge_id"] == "e1"
    assert loop["resolved_exit_edges"] == ["e2"]
    assert loop["frames"] == ["settled", "settled"]


def test_every_generated_back_edge_runs_a_production_loop_transition() -> None:
    case = Case(
        Topology(
            ("n0", "n1", "n2", "n3"),
            (
                Edge("e0", "n0", "n1", 0),
                Edge("e1", "n1", "n0", 0),
                Edge("e2", "n1", "n2", 0),
                Edge("e3", "n2", "n1", 0),
                Edge("e4", "n2", "n3", 0),
            ),
        ),
        (True,) * 5,
        (),
        State("null", "collect", "none", "none", "none"),
    )

    production = ProductionAdapter().run(case).persisted_state["production"]

    assert [loop["back_edge_id"] for loop in production["loops"]] == ["e1", "e3"]
    assert production["loops"][0]["resolved_exit_edges"] == ["e2"]
    assert production["loops"][1]["resolved_exit_edges"] == ["e4"]


def test_every_generated_join_cohort_runs_a_production_join_transition() -> None:
    case = Case(
        Topology(
            ("n0", "n1", "n2", "n3", "n4"),
            (
                Edge("e0", "n0", "n1", 0),
                Edge("e1", "n0", "n2", 0),
                Edge("e2", "n1", "n3", 0),
                Edge("e3", "n2", "n3", 0),
                Edge("e4", "n1", "n4", 0),
                Edge("e5", "n3", "n4", 0),
            ),
        ),
        (True,) * 6,
        (),
        State("null", "collect", "fail-first", "none", "none"),
    )

    production = ProductionAdapter().run(case).persisted_state["production"]

    assert [join["edge_ids"] for join in production["joins"]] == [
        ["e2", "e3"],
        ["e4", "e5"],
    ]
    assert all(join["state"] == "closed" for join in production["joins"])


def test_generated_graph_reaches_production_terminal_state() -> None:
    production = ProductionAdapter().run(_structured_case()).persisted_state["production"]

    assert production["graph_execution"]["pending_token_ids"] == []
    assert production["graph_execution"]["state"] == "stopped"


def test_cancellation_materially_changes_compared_lifecycle_trace() -> None:
    topology = next(generate_topologies(4))
    uncancelled = next(
        case
        for case in enumerate_cases(topology)
        if case.state.checkpoint == "after-claim" and case.state.cancellation == "none"
    )
    cancelled = next(
        case
        for case in enumerate_cases(topology)
        if case.state.checkpoint == "after-claim" and case.state.cancellation == "after-cut"
    )

    first = ProductionAdapter().run(uncancelled)
    second = ProductionAdapter().run(cancelled)

    assert first.persisted_state["production"]["lifecycle"]["state"] != (
        second.persisted_state["production"]["lifecycle"]["state"]
    )
    assert first.persisted_state["production"]["graph_execution"]["cancelled"] is False
    assert second.persisted_state["production"]["graph_execution"]["cancelled"] is True
    assert second.persisted_state["production"]["graph_execution"]["state"] == "cancelled"
    assert normalize_trace(first) != normalize_trace(second)


def test_ready_width_six_is_schedule_exhaustive() -> None:
    orders = schedule_orders(tuple("abcdef"), seed=9, case_digest="case")

    assert len(orders) == 720
    assert len(set(orders)) == 720


def test_schedule_discovery_reports_actual_ready_token_ids() -> None:
    case = Case(
        Topology(
            ("n0", "n1", "n2", "n3"),
            (
                Edge("e0", "n0", "n1", 0),
                Edge("e1", "n0", "n2", 0),
                Edge("e2", "n1", "n3", 0),
                Edge("e3", "n2", "n3", 0),
            ),
        ),
        (True, True, True, True),
        (),
        State("null", "collect", "none", "none", "none"),
    )

    ready_sets = Oracle().ready_sets(case)

    assert ready_sets
    assert all(token_id.startswith("t0.") for ready in ready_sets for token_id in ready)
    assert all(not token_id.startswith("e") for ready in ready_sets for token_id in ready)


def test_wide_ready_set_uses_canonical_reverse_and_seeded_orders() -> None:
    ready = tuple("abcdefg")

    orders = schedule_orders(ready, seed=120600, case_digest="case")

    assert orders[0] == ready
    assert orders[1] == tuple(reversed(ready))
    assert orders == schedule_orders(ready, seed=120600, case_digest="case")
    assert len(orders) == 3


def test_every_observed_ready_state_contributes_schedule_choices() -> None:
    plans = schedule_plans(
        (("t-left", "t-right"), ("t-a", "t-b", "t-c")),
        seed=120600,
        case_digest="case",
    )

    assert len(plans) == 8
    assert set(plans[:2]) == {("t-left", "t-right"), ("t-right", "t-left")}
    assert set(plans[2:]) == set(permutations(("t-a", "t-b", "t-c")))


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


def test_convergent_terminal_persistence_is_schedule_order_independent() -> None:
    topology = Topology(
        tuple(f"n{index}" for index in range(5)),
        (
            Edge("e0", "n0", "n1", 0, "c0", True),
            Edge("e1", "n0", "n2", 0, "c0", False),
            Edge("e2", "n0", "n3", 0, "c0", True),
            Edge("e3", "n1", "n3", 0),
            Edge("e4", "n2", "n4", 0),
            Edge("e5", "n3", "n2", 0),
        ),
    )
    case = Case(
        topology,
        (True, False, True, True, True, True),
        (("c0", True),),
        State('{"p":1}', "last", "fail-first", "before-dispatch", "none"),
    )

    expected = normalize_trace(Oracle().run(case, ("e0", "e2")))
    observed = normalize_trace(ProductionAdapter().run(case, ("e0", "e2")))

    assert observed == expected
