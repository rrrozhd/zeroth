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


def test_subgraph_publishes_its_whole_surface() -> None:
    from zeroth.runtime import subgraphs as canonical

    for name in EXPORTS:
        assert hasattr(canonical, name), name


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
def test_subgraph_modules_publish_their_names(
    module_name: str, names: tuple[str, ...]
) -> None:
    canonical_module = importlib.import_module(f"zeroth.runtime.subgraphs.{module_name}")

    for name in names:
        assert hasattr(canonical_module, name), name


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


def test_subgraphs_imports_in_a_cold_interpreter() -> None:
    """The canonical package imports with nothing else pre-warmed.

    This kept the canonical half of a test that used to import the legacy
    and canonical packages in both orders, guarding a cycle between them.
    With the legacy package gone there is one direction left to guard.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import zeroth.runtime.subgraphs"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
