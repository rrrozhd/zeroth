from __future__ import annotations

import inspect

import pytest

from scripts.token_engine_checker.generator import enumerate_cases, generate_topologies
from scripts.token_engine_checker.models import Case
from scripts.token_engine_checker.oracle import Oracle, OracleViolation


def _case() -> Case:
    topology = next(topology for topology in generate_topologies(4) if len(topology.edges) > 3)
    return next(case for case in enumerate_cases(topology) if all(case.enabled))


def test_oracle_has_no_runtime_imports() -> None:
    source = inspect.getsource(inspect.getmodule(Oracle))

    assert "zeroth.runtime" not in source


def test_oracle_produces_edge_labelled_terminal_trace() -> None:
    trace = Oracle().run(_case())

    assert trace.resolutions
    assert all(event.edge_id.startswith("e") for event in trace.resolutions)
    assert trace.terminal_output is not None
    assert not trace.pending


def test_oracle_rejects_duplicate_edge_resolution_mutation() -> None:
    trace = Oracle().run(_case())
    mutated = trace.with_resolutions((*trace.resolutions, trace.resolutions[0]))

    with pytest.raises(OracleViolation, match="duplicate edge resolution"):
        Oracle().validate(mutated)
