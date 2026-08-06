"""Enforce backend domain dependency direction across the source tree."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
SOURCE_ROOT = REPO_ROOT / "src"


def _write_module(source_root: Path, module_name: str, source: str) -> Path:
    path = source_root.joinpath(*module_name.split(".")).with_suffix(".py")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    return path


def test_dependency_matrix_matches_the_approved_domain_policy() -> None:
    architecture = importlib.import_module("zeroth._architecture")

    assert {
        "platform": frozenset(),
        "contracts": frozenset({"platform"}),
        "governance": frozenset({"contracts", "platform"}),
        "runtime": frozenset({"contracts", "governance", "platform"}),
        "econ": frozenset({"contracts", "platform"}),
        "integrations": frozenset({"contracts", "governance", "platform", "runtime", "econ"}),
        "service": frozenset(
            {
                "contracts",
                "econ",
                "eval",
                "governance",
                "integrations",
                "platform",
                "runtime",
            }
        ),
        "eval": frozenset({"contracts", "runtime", "platform"}),
    } == architecture.ALLOWED_DEPENDENCIES


def test_scanner_reports_an_injected_forbidden_absolute_import(tmp_path: Path) -> None:
    architecture = importlib.import_module("zeroth._architecture")
    source_root = tmp_path / "src"
    module = _write_module(source_root, "zeroth.platform.clock", "import zeroth.runtime.runs\n")

    violations = architecture.find_dependency_violations(source_root)

    assert violations[0].path == module
    assert [(item.importer, item.line, item.imported) for item in violations] == [
        ("zeroth.platform.clock", 1, "zeroth.runtime.runs")
    ]


def test_scanner_normalizes_relative_imports_to_absolute_modules(tmp_path: Path) -> None:
    architecture = importlib.import_module("zeroth._architecture")
    source_root = tmp_path / "src"
    _write_module(
        source_root,
        "zeroth.runtime.orchestration.driver",
        "from ...service import api\n",
    )

    violations = architecture.find_dependency_violations(source_root)

    assert [(item.importer, item.line, item.imported) for item in violations] == [
        ("zeroth.runtime.orchestration.driver", 1, "zeroth.service")
    ]


def test_importfrom_resolves_top_level_package_alias(tmp_path: Path) -> None:
    architecture = importlib.import_module("zeroth._architecture")
    source_root = tmp_path / "src"
    _write_module(source_root, "zeroth.runtime.__init__", "")
    _write_module(
        source_root,
        "zeroth.platform.clock",
        "from zeroth import runtime\n",
    )

    violations = architecture.find_dependency_violations(source_root)

    assert [(item.importer, item.imported) for item in violations] == [
        ("zeroth.platform.clock", "zeroth.runtime")
    ]


def test_importfrom_resolves_nested_module_alias(tmp_path: Path) -> None:
    architecture = importlib.import_module("zeroth._architecture")
    source_root = tmp_path / "src"
    _write_module(source_root, "zeroth.integrations.persistence.repository", "")
    _write_module(
        source_root,
        "zeroth.runtime.driver",
        "from zeroth.integrations.persistence import repository\n",
    )

    violations = architecture.find_dependency_violations(source_root)

    # Both edges are reported: the package named in the ``from`` clause and the
    # submodule the alias resolves to. The second is the one this test is about --
    # without alias resolution the scanner would only ever see the package.
    assert [(item.importer, item.imported) for item in violations] == [
        ("zeroth.runtime.driver", "zeroth.integrations.persistence"),
        ("zeroth.runtime.driver", "zeroth.integrations.persistence.repository"),
    ]


def test_package_init_importer_is_normalized_when_resolving_alias(
    tmp_path: Path,
) -> None:
    architecture = importlib.import_module("zeroth._architecture")
    source_root = tmp_path / "src"
    _write_module(source_root, "zeroth.runtime.runs", "")
    _write_module(
        source_root,
        "zeroth.platform.__init__",
        "from zeroth.runtime import runs\n",
    )

    violations = architecture.find_dependency_violations(source_root)

    assert [(item.importer, item.imported) for item in violations] == [
        ("zeroth.platform", "zeroth.runtime"),
        ("zeroth.platform", "zeroth.runtime.runs"),
    ]


def test_relative_importfrom_resolves_child_module_alias(tmp_path: Path) -> None:
    architecture = importlib.import_module("zeroth._architecture")
    source_root = tmp_path / "src"
    _write_module(source_root, "zeroth.contracts.graph.repository", "import zeroth.service.app\n")
    _write_module(
        source_root,
        "zeroth.contracts.graph.__init__",
        "from . import repository\n",
    )

    violations = architecture.find_dependency_violations(source_root, exceptions={})

    assert [(item.importer, item.imported) for item in violations] == [
        ("zeroth.contracts.graph.repository", "zeroth.service.app")
    ]


def test_importfrom_does_not_treat_symbol_alias_as_a_module(tmp_path: Path) -> None:
    architecture = importlib.import_module("zeroth._architecture")
    source_root = tmp_path / "src"
    _write_module(source_root, "zeroth.__init__", "service = object()\n")
    _write_module(
        source_root,
        "zeroth.runtime.driver",
        "from zeroth import service\n",
    )

    violations = architecture.find_dependency_violations(source_root)

    assert not violations


def test_allowed_and_disallowed_edges_follow_the_matrix(tmp_path: Path) -> None:
    architecture = importlib.import_module("zeroth._architecture")
    source_root = tmp_path / "src"
    _write_module(
        source_root,
        "zeroth.runtime.driver",
        "import json\nimport zeroth.runtime.runs\nimport zeroth.contracts.graph\n",
    )
    _write_module(
        source_root,
        "zeroth.contracts.graph",
        "import zeroth.governance.policy\n",
    )

    violations = architecture.find_dependency_violations(source_root)

    assert [(item.importer, item.line, item.imported) for item in violations] == [
        ("zeroth.contracts.graph", 1, "zeroth.governance.policy")
    ]


def test_scanner_parses_unclassified_modules_and_reports_syntax_errors(
    tmp_path: Path,
) -> None:
    architecture = importlib.import_module("zeroth._architecture")
    source_root = tmp_path / "src"
    module = _write_module(
        source_root,
        "zeroth.contracts.examples.broken",
        "def broken(:\n",
    )

    with pytest.raises(SyntaxError) as caught:
        architecture.scan_backend_dependencies(source_root)

    assert caught.value.filename == str(module)
    assert caught.value.lineno == 1


def test_temporary_exceptions_match_the_exact_importer_and_imported_module(
    tmp_path: Path,
) -> None:
    architecture = importlib.import_module("zeroth._architecture")
    source_root = tmp_path / "src"
    _write_module(
        source_root,
        "zeroth.platform.clock",
        "import zeroth.runtime.runs\nimport zeroth.runtime.agents\n",
    )
    exceptions = {
        ("zeroth.platform.clock", "zeroth.runtime.runs"): architecture.DependencyException(
            reason="Legacy clock still imports the run model during migration.",
            removal_task="Task 4: move clocks to platform primitives.",
        )
    }

    violations = architecture.find_dependency_violations(source_root, exceptions=exceptions)

    assert [(item.importer, item.imported) for item in violations] == [
        ("zeroth.platform.clock", "zeroth.runtime.agents")
    ]


def test_repository_exceptions_are_documented_exact_current_edges() -> None:
    architecture = importlib.import_module("zeroth._architecture")
    unexcepted = architecture.scan_backend_dependencies(SOURCE_ROOT, exceptions={})
    current_edges = {(item.importer, item.imported) for item in unexcepted.violations}

    assert set(architecture.TEMPORARY_EXCEPTIONS) == current_edges
    for exception in architecture.TEMPORARY_EXCEPTIONS.values():
        assert exception.reason.strip()
        assert exception.removal_task.startswith("Task ")


def test_real_repository_obeys_backend_dependency_direction() -> None:
    architecture = importlib.import_module("zeroth._architecture")
    scan = architecture.scan_backend_dependencies(SOURCE_ROOT)
    expected_files = tuple(sorted((SOURCE_ROOT / "zeroth").rglob("*.py")))

    assert scan.scanned_files == expected_files
    assert not scan.violations, "\n".join(
        f"{item.path.relative_to(REPO_ROOT)}:{item.line}: {item.importer} imports {item.imported}"
        for item in scan.violations
    )




def test_every_langgraph_gateway_file_on_disk_classifies_to_a_domain() -> None:
    architecture = importlib.import_module("zeroth._architecture")
    gateway_root = SOURCE_ROOT / "zeroth" / "core" / "langgraph_gateway"

    for path in sorted(gateway_root.glob("*.py")):
        module, _ = architecture._module_name(path, SOURCE_ROOT)
        assert architecture._canonical_domain(module) is not None, module


def test_langgraph_gateway_scan_surfaces_exactly_the_forbidden_edges() -> None:
    architecture = importlib.import_module("zeroth._architecture")
    scan = architecture.scan_backend_dependencies(SOURCE_ROOT, exceptions={})
    gateway_edges = {
        (item.importer, item.imported)
        for item in scan.violations
        if "langgraph_gateway" in item.importer or "langgraph_gateway" in item.imported
    }

    # ZER-24 removed both gateway exceptions by moving the dependencies rather
    # than relocating them: S2 inverted admission onto a governance-owned
    # evaluator, and S3a moved the enforcement wire protocol into
    # ``integrations``, where the client reaches it without leaving its own
    # domain. Neither produces a permitted edge -- there is no edge at all.
    #
    # The filter matches ``langgraph_gateway`` anywhere in either endpoint. It
    # used to match the ``zeroth.core.langgraph_gateway`` prefix, which ZER-25
    # deleted -- leaving a set that was empty because nothing could ever match
    # it, rather than because the edges are gone.
    assert gateway_edges == set()


def test_langgraph_gateway_exceptions_are_the_only_gateway_entries_and_documented() -> None:
    architecture = importlib.import_module("zeroth._architecture")
    gateway_exceptions = {
        edge
        for edge in architecture.TEMPORARY_EXCEPTIONS
        if "langgraph_gateway" in edge[0] + edge[1]
    }

    assert gateway_exceptions == set()
    for edge in sorted(gateway_exceptions):
        exception = architecture.TEMPORARY_EXCEPTIONS[edge]
        assert exception.reason.strip()
        assert exception.removal_task.startswith("Task ")


def test_domain_classification_has_no_legacy_prefix_table() -> None:
    """A module's domain comes from its own path, with no shim indirection.

    While ``LEGACY_DOMAIN_PREFIXES`` existed, a dependency that went through a
    ``zeroth.core`` shim was classified as if it went straight to the canonical
    package, so real cross-domain edges never surfaced. ZER-25 deleted both the
    shims and the table; this pins that it does not come back.
    """
    architecture = importlib.import_module("zeroth._architecture")

    assert not hasattr(architecture, "LEGACY_DOMAIN_PREFIXES")
    assert architecture._canonical_domain("zeroth.runtime.runs") == "runtime"
    assert architecture._canonical_domain("zeroth.core.runs") is None


def test_the_relocated_gateway_modules_classify_by_their_canonical_paths() -> None:
    """The gateway's domains come from where its modules now live.

    This pinned the legacy-prefix table's gateway rows, which mapped each
    ``zeroth.core.langgraph_gateway`` submodule to the domain that would own it
    after ZER-24. Both the shims and the table are gone, so the same mapping is
    asserted against the canonical paths instead.
    """
    architecture = importlib.import_module("zeroth._architecture")

    assert {
        module: architecture._canonical_domain(module)
        for module in (
            "zeroth.contracts.langgraph_gateway.models",
            "zeroth.contracts.langgraph_gateway.inventory",
            "zeroth.governance.langgraph_gateway.capabilities",
            "zeroth.governance.langgraph_gateway.events",
            "zeroth.service.langgraph_gateway.proxy",
            "zeroth.service.langgraph_gateway.routes",
            "zeroth.integrations.langgraph.enforcement_protocol",
        )
    } == {
        "zeroth.contracts.langgraph_gateway.models": "contracts",
        "zeroth.contracts.langgraph_gateway.inventory": "contracts",
        "zeroth.governance.langgraph_gateway.capabilities": "governance",
        "zeroth.governance.langgraph_gateway.events": "governance",
        "zeroth.service.langgraph_gateway.proxy": "service",
        "zeroth.service.langgraph_gateway.routes": "service",
        "zeroth.integrations.langgraph.enforcement_protocol": "integrations",
    }
