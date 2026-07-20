"""Canonical import surface for the runtime subgraphs package.

Non-golden boundary tests for the Task 14 subgraph move: the canonical
``zeroth.runtime.subgraphs`` package must publish the same objects the legacy
``zeroth.core.subgraph`` path keeps republishing, and both packages must stay
cold-importable from a fresh interpreter in either order.
"""

from __future__ import annotations

import importlib
import subprocess
import sys

import pytest


EXPORTS = (
    "SubgraphCycleError",
    "SubgraphDepthLimitError",
    "SubgraphError",
    "SubgraphExecutionError",
    "SubgraphExecutor",
    "SubgraphNodeData",
    "SubgraphResolutionError",
)


def test_subgraph_is_the_same_surface_through_both_paths() -> None:
    from zeroth.core import subgraph as legacy
    from zeroth.runtime import subgraphs as canonical

    for name in EXPORTS:
        assert getattr(canonical, name) is getattr(legacy, name), name


@pytest.mark.parametrize(
    ("module_name", "names"),
    [
        (
            "errors",
            (
                "SubgraphCycleError",
                "SubgraphDepthLimitError",
                "SubgraphError",
                "SubgraphExecutionError",
                "SubgraphResolutionError",
            ),
        ),
        ("executor", ("SubgraphExecutor",)),
        ("models", ("SubgraphNodeData",)),
        (
            "resolver",
            (
                "SubgraphResolver",
                "base_node_id",
                "merge_governance",
                "namespace_subgraph",
            ),
        ),
    ],
)
def test_subgraph_modules_are_the_same_surface_through_both_paths(
    module_name: str, names: tuple[str, ...]
) -> None:
    legacy_module = importlib.import_module(f"zeroth.core.subgraph.{module_name}")
    canonical_module = importlib.import_module(f"zeroth.runtime.subgraphs.{module_name}")

    for name in names:
        assert getattr(canonical_module, name) is getattr(legacy_module, name), name


def test_subgraph_node_data_remains_the_contract_owned_model() -> None:
    from zeroth.contracts.graph.models import SubgraphNodeData as ContractSubgraphNodeData
    from zeroth.runtime.subgraphs.models import SubgraphNodeData as RuntimeSubgraphNodeData

    assert RuntimeSubgraphNodeData is ContractSubgraphNodeData


def test_subgraph_resolver_carries_no_deployment_service_import() -> None:
    """The resolver reaches deployments only through its runtime-owned protocol."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "import zeroth.runtime.subgraphs.resolver\n"
            "assert 'zeroth.core.deployments' not in sys.modules, 'deployments loaded'\n"
            "assert 'zeroth.core.deployments.service' not in sys.modules\n",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("zeroth.runtime.subgraphs", "zeroth.core.subgraph"),
        ("zeroth.core.subgraph", "zeroth.runtime.subgraphs"),
    ],
)
def test_subgraph_cold_imports_from_both_directions(first: str, second: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}\nimport {second}\n"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"cold import {first} then {second} failed:\n{result.stderr}"
