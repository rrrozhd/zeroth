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
    _write_module(source_root, "zeroth.core.runs.repository", "")
    _write_module(
        source_root,
        "zeroth.runtime.driver",
        "from zeroth.core.runs import repository\n",
    )

    violations = architecture.find_dependency_violations(source_root)

    assert [(item.importer, item.imported) for item in violations] == [
        ("zeroth.runtime.driver", "zeroth.core.runs.repository")
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
    _write_module(source_root, "zeroth.core.runs.repository", "")
    _write_module(
        source_root,
        "zeroth.core.runs.__init__",
        "from . import repository\n",
    )

    violations = architecture.find_dependency_violations(source_root, exceptions={})

    assert [(item.importer, item.imported) for item in violations] == [
        ("zeroth.core.runs", "zeroth.core.runs.repository")
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
        "zeroth.core.examples.broken",
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
