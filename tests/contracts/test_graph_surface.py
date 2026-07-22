"""Canonical import surface for the graph contracts package.

Non-golden boundary tests for the Task 12 graph move: the canonical
``zeroth.contracts.graph`` package must publish the same objects the legacy
``zeroth.core.graph`` path keeps republishing, and both packages must stay
cold-importable from a fresh interpreter in either order.

Three authored node-configuration models are separated from their
runtime-owned packages and defined in ``zeroth.contracts.graph.models``:
``SubgraphNodeData``, ``ParallelConfig``, and ``ContextWindowSettings`` are
graph-authoring vocabulary embedded in node data, while the executors that
consume them stay behind their legacy packages, which republish the same
objects in the allowed runtime -> contracts direction.
"""

from __future__ import annotations

import subprocess
import sys

import pytest


def test_graph_is_the_same_surface_through_both_paths() -> None:
    from zeroth.contracts import graph as canonical
    from zeroth.core import graph as legacy

    for name in (
        "AgentNode",
        "AgentNodeData",
        "AgentToolBinding",
        "Condition",
        "DisplayMetadata",
        "Edge",
        "EntrypointNode",
        "EntrypointNodeData",
        "ExecutableUnitNode",
        "ExecutableUnitNodeData",
        "ExecutionSettings",
        "Graph",
        "GraphRepository",
        "GraphStatus",
        "HumanApprovalNode",
        "HumanApprovalNodeData",
        "Node",
        "RetrievalNode",
        "RetrievalNodeData",
        "SubgraphNode",
        "SubgraphNodeData",
        "TemplateMemoryBinding",
        "ToolArgument",
    ):
        assert getattr(canonical, name) is getattr(legacy, name), name


def test_graph_submodules_are_the_same_surface_through_both_paths() -> None:
    from zeroth.contracts.graph import errors as canonical_errors
    from zeroth.contracts.graph import models as canonical_models
    from zeroth.contracts.graph import validation_errors as canonical_validation_errors
    from zeroth.core.graph import errors as legacy_errors
    from zeroth.core.graph import models as legacy_models
    from zeroth.core.graph import validation_errors as legacy_validation_errors

    assert canonical_errors.GraphLifecycleError is legacy_errors.GraphLifecycleError
    assert canonical_models.Graph is legacy_models.Graph
    assert canonical_models.NodeBase is legacy_models.NodeBase
    assert (
        canonical_validation_errors.GraphValidationError
        is legacy_validation_errors.GraphValidationError
    )
    assert canonical_validation_errors.ValidationIssue is legacy_validation_errors.ValidationIssue


def test_separated_node_models_have_one_contract_owned_definition() -> None:
    from zeroth.contracts.graph import models as canonical
    from zeroth.core.context_window import models as context_window_models
    from zeroth.core.parallel import models as parallel_models
    from zeroth.core.subgraph import models as subgraph_models

    assert subgraph_models.SubgraphNodeData is canonical.SubgraphNodeData
    assert parallel_models.ParallelConfig is canonical.ParallelConfig
    assert context_window_models.ContextWindowSettings is canonical.ContextWindowSettings


def test_graph_validator_stays_runtime_owned_and_lazily_republished() -> None:
    from zeroth.core.graph import validation as legacy_validation
    from zeroth.runtime.graph_validation import GraphValidator

    assert legacy_validation.GraphValidator is GraphValidator


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("zeroth.contracts.graph", "zeroth.core.graph"),
        ("zeroth.core.graph", "zeroth.contracts.graph"),
        ("zeroth.contracts.graph", "zeroth.core.subgraph"),
        ("zeroth.core.subgraph", "zeroth.contracts.graph"),
        ("zeroth.contracts.graph", "zeroth.core.parallel"),
        ("zeroth.core.parallel", "zeroth.contracts.graph"),
        ("zeroth.contracts.graph", "zeroth.core.context_window"),
        ("zeroth.core.context_window", "zeroth.contracts.graph"),
    ],
)
def test_graph_cold_imports_from_both_directions(first: str, second: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}\nimport {second}\n"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"cold import {first} then {second} failed:\n{result.stderr}"
