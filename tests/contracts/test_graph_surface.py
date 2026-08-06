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


def test_graph_publishes_its_whole_surface() -> None:
    from zeroth.contracts import graph as canonical

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
        assert hasattr(canonical, name), name


def test_graph_submodules_publish_their_names() -> None:
    from zeroth.contracts.graph import errors as canonical_errors
    from zeroth.contracts.graph import models as canonical_models
    from zeroth.contracts.graph import validation_errors as canonical_validation_errors
    from zeroth.core.graph import validation_errors as legacy_validation_errors

    assert hasattr(canonical_errors, "GraphLifecycleError")
    assert hasattr(canonical_models, "Graph")
    assert hasattr(canonical_models, "NodeBase")
    assert (
        canonical_validation_errors.GraphValidationError
        is legacy_validation_errors.GraphValidationError
    )
    assert hasattr(canonical_validation_errors, "ValidationIssue")


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


def test_models_imports_in_a_cold_interpreter() -> None:
    """The canonical package imports with nothing else pre-warmed.

    This kept the canonical half of a test that used to import the legacy
    and canonical packages in both orders, guarding a cycle between them.
    With the legacy package gone there is one direction left to guard.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import zeroth.contracts.graph.models"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
