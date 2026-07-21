from __future__ import annotations

from dataclasses import replace

from scripts.token_engine_checker.generator import (
    enumerate_cases,
    generate_topologies,
    sample_cases,
)
from scripts.token_engine_checker.models import (
    Case,
    Edge,
    State,
    Topology,
    canonicalize_edges,
    classify_case,
    classify_topology,
)


def test_parallel_edges_receive_stable_canonical_ids() -> None:
    edges = canonicalize_edges((("n1", "n2"), ("n0", "n1"), ("n1", "n2"), ("n0", "n2")))

    assert [(edge.edge_id, edge.source, edge.target, edge.parallel_ordinal) for edge in edges] == [
        ("e0", "n0", "n1", 0),
        ("e1", "n0", "n2", 0),
        ("e2", "n1", "n2", 0),
        ("e3", "n1", "n2", 1),
    ]


def test_topology_classifier_rejects_a_third_parallel_edge() -> None:
    topology = Topology(
        nodes=("n0", "n1", "n2"),
        edges=(
            Edge("e0", "n0", "n1", 0),
            Edge("e1", "n0", "n1", 1),
            Edge("e2", "n0", "n1", 2),
            Edge("e3", "n1", "n2", 0),
        ),
    )

    classification = classify_topology(topology)

    assert not classification.valid
    assert classification.reason == "parallel_edge_bound"


def test_generator_is_finite_unique_and_includes_parallel_edges() -> None:
    topologies = tuple(generate_topologies(4))

    assert topologies
    assert len(topologies) == len({topology.digest for topology in topologies})
    assert any(
        len({(edge.source, edge.target) for edge in topology.edges}) < len(topology.edges)
        for topology in topologies
    )
    assert all(classify_topology(topology).valid for topology in topologies)


def test_case_classifier_rejects_cancellation_without_checkpoint() -> None:
    topology = next(generate_topologies(4))
    base = next(enumerate_cases(topology))
    broken = replace(base, state=replace(base.state, checkpoint="none", cancellation="after-cut"))

    classification = classify_case(broken)

    assert not classification.valid
    assert classification.reason == "cancellation_without_checkpoint"


def test_case_domain_contains_all_finite_atoms() -> None:
    topology = next(generate_topologies(4))
    states = {case.state for case in enumerate_cases(topology)}

    assert {state.payload_json for state in states} == {"null", "false", "0", '{"p":1}'}
    assert {state.reducer for state in states} == {"collect", "merge", "last"}
    assert {state.retry for state in states} == {"none", "fail-first"}
    assert {state.checkpoint for state in states} == {
        "none",
        "before-claim",
        "after-claim",
        "after-resolve",
        "before-dispatch",
    }


def test_sampling_is_deterministic_and_without_replacement() -> None:
    first = sample_cases(5, count=40, seed=120500)
    second = sample_cases(5, count=40, seed=120500)

    assert [case.digest for case in first] == [case.digest for case in second]
    assert len({case.digest for case in first}) == 40


def test_valid_case_rejects_unknown_state_atom() -> None:
    topology = next(generate_topologies(4))
    case = Case(
        topology=topology,
        enabled=(True,) * len(topology.edges),
        conditions=(),
        state=State("null", "invented", "none", "none", "none"),
    )

    assert classify_case(case).reason == "unknown_reducer"
