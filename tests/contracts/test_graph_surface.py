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

    assert hasattr(canonical_errors, "GraphLifecycleError")
    assert hasattr(canonical_models, "Graph")
    assert hasattr(canonical_models, "NodeBase")
    assert hasattr(canonical_validation_errors, "GraphValidationError")
    assert hasattr(canonical_validation_errors, "ValidationIssue")


def test_separated_node_models_have_one_contract_owned_definition() -> None:
    """The node models the runtime packages expose are the contract's own objects.

    The comparison used to run through the legacy republishers; it now runs
    against the canonical runtime packages, which is what it was always really
    asserting -- that these packages do not fork the contract's definitions.
    """
    from zeroth.contracts.graph import models as canonical
    from zeroth.runtime.context import models as context_window_models
    from zeroth.runtime.parallel import models as parallel_models
    from zeroth.runtime.subgraphs import models as subgraph_models

    assert subgraph_models.SubgraphNodeData is canonical.SubgraphNodeData
    assert parallel_models.ParallelConfig is canonical.ParallelConfig
    assert context_window_models.ContextWindowSettings is canonical.ContextWindowSettings


def test_graph_validator_stays_runtime_owned() -> None:
    """``GraphValidator`` is defined by the runtime, not by the contracts layer.

    The legacy ``zeroth.core.graph.validation`` facade this used to compare
    against is gone; the ownership it was pinning is asserted directly.
    """
    from zeroth.runtime.graph_validation import GraphValidator

    assert GraphValidator.__module__ == "zeroth.runtime.graph_validation"


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
