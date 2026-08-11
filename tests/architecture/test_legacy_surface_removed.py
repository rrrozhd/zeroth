"""The retired ``zeroth.core`` / ``zeroth.econ_plane`` surfaces are gone for good.

ZER-25 removed both trees outright. While the conversion was in progress this
module carried a baseline listing the files that still imported them, which was
allowed to shrink but never grow. The baseline reached zero, so it and the
allowlist machinery were deleted with the trees: what remains is the flat
prohibition, with no exemptions to erode.

Every import assertion here runs in a subprocess. ``tests/conftest.py`` imports
service bootstrap at collection time, so by the time an in-process test body
runs, most of the package graph is already warm and an import question cannot be
answered honestly from inside it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tests.architecture.legacy_imports import (
    LEGACY_ROOTS,
    REPO_ROOT,
    is_legacy_module,
    legacy_imports_in,
    scan_repository,
)


def _cold(code: str) -> subprocess.CompletedProcess[str]:
    """Run ``code`` in a fresh interpreter."""
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)


def test_the_ast_scanner_finds_no_file_importing_a_retired_module() -> None:
    """The flat prohibition: nothing under any scanned tree may import them."""
    offenders = scan_repository()

    assert not offenders, "these files import a retired module:\n  " + "\n  ".join(
        f"{path}:{line} imports {module}"
        for path, hits in sorted(offenders.items())
        for line, module in hits
    )


def test_find_spec_resolves_no_retired_package_in_a_fresh_interpreter() -> None:
    """A consumer cannot import them, however the process was started."""
    result = _cold(
        "import importlib.util\n"
        "resolved = [\n"
        "    name\n"
        "    for name in ('zeroth.core', 'zeroth.econ_plane')\n"
        "    if importlib.util.find_spec(name) is not None\n"
        "]\n"
        "assert not resolved, resolved\n"
    )

    assert result.returncode == 0, f"retired packages still resolve:\n{result.stderr}"


def test_the_retired_trees_are_absent_from_the_source_checkout() -> None:
    """Deleted, not merely unreachable."""
    for tree in ("src/zeroth/core", "src/zeroth/econ_plane"):
        assert not (REPO_ROOT / tree).exists(), f"{tree} still exists"


def test_backend_domains_cold_import_without_loading_a_retired_module() -> None:
    """Each canonical package stands up cold, touching nothing retired.

    This is the check the task's own risk section calls for: the suite's
    ``conftest`` used to warm ``zeroth.core`` transitively, so an in-process
    assertion proved nothing about a real consumer.
    """
    from zeroth._architecture import BACKEND_DOMAINS

    modules = sorted(f"zeroth.{domain}" for domain in BACKEND_DOMAINS)
    result = _cold(
        "import sys\n"
        f"for name in {modules!r}:\n"
        "    __import__(name)\n"
        "leaked = [n for n in sys.modules if n.startswith(('zeroth.core', 'zeroth.econ_plane'))]\n"
        "assert not leaked, leaked\n"
    )

    assert result.returncode == 0, f"a backend domain reaches a retired module:\n{result.stderr}"


def test_the_ast_scanner_detects_every_import_form(tmp_path: Path) -> None:
    """A guard that cannot see an offender would pass vacuously forever.

    Covers the four shapes the repository used, plus the two kinds of false
    positive that make a substring search useless here: prose naming the old
    path, and a canonical module whose name merely starts the same way.
    """
    module = tmp_path / "sample.py"
    module.write_text(
        '"""Docstring mentioning zeroth.core.runs, which is not an import."""\n'
        "import zeroth.core\n"
        "import zeroth.econ_plane.config\n"
        "from zeroth.core.runs import RunRepository\n"
        "from zeroth.core.audit.models import AuditRecord\n"
        "import zeroth.runtime.runs\n"
        "from zeroth.contracts.graph import Capability\n"
        'LEGACY = "zeroth.core.cli"\n',
        encoding="utf-8",
    )

    assert [name for _, name in legacy_imports_in(module)] == [
        "zeroth.core",
        "zeroth.econ_plane.config",
        "zeroth.core.runs",
        "zeroth.core.audit.models",
    ]


def test_a_canonical_module_sharing_a_prefix_is_not_a_legacy_module() -> None:
    """``zeroth.core`` matches itself and its descendants -- nothing else."""
    assert LEGACY_ROOTS == ("zeroth.core", "zeroth.econ_plane")
    assert is_legacy_module("zeroth.core")
    assert is_legacy_module("zeroth.core.runs.models")
    assert is_legacy_module("zeroth.econ_plane")
    assert not is_legacy_module("zeroth.econ")
    assert not is_legacy_module("zeroth.econ.plane")
    assert not is_legacy_module("zeroth.corex")
    assert not is_legacy_module(None)


def test_alembic_resolves_migrations_only_from_the_service_domain() -> None:
    """No configuration may still point Alembic at the retired package."""
    config = (REPO_ROOT / "alembic.ini").read_text(encoding="utf-8")
    bootstrap = (REPO_ROOT / "src/zeroth/service/bootstrap/migrations.py").read_text(
        encoding="utf-8"
    )

    assert "script_location = src/zeroth/service/_migrations" in config
    assert 'importlib.resources.files("zeroth.service._migrations")' in bootstrap
    for text, name in ((config, "alembic.ini"), (bootstrap, "bootstrap/migrations.py")):
        assert "core/migrations" not in text, name
        assert "core.migrations" not in text, name


def test_alembic_upgrades_a_scratch_database_to_head(tmp_path: Path) -> None:
    """The relocated revision tree still applies end to end."""
    from zeroth.service.bootstrap.migrations import run_migrations

    database = tmp_path / "scratch.db"
    run_migrations(f"sqlite:///{database}")

    import sqlite3

    with sqlite3.connect(database) as connection:
        applied = [row[0] for row in connection.execute("select * from alembic_version")]
        tables = {
            row[0]
            for row in connection.execute("select name from sqlite_master where type='table'")
        }

    assert applied == ["022"]
    assert "runs" in tables
    assert "side_effect_operations" in tables


def test_the_orchestrator_is_defined_in_the_runtime_domain() -> None:
    """``RuntimeOrchestrator`` is owned by the runtime, not by a legacy module."""
    from zeroth.runtime.orchestration import RuntimeOrchestrator

    assert RuntimeOrchestrator.__module__ == "zeroth.runtime.orchestration.orchestrator"


def test_importing_light_runtime_contracts_does_not_load_the_orchestrator() -> None:
    """The orchestrator stays lazy behind the runtime package surface."""
    result = _cold(
        "import sys\n"
        "import zeroth.runtime.runs\n"
        "import zeroth.runtime.orchestration\n"
        "eager = [\n"
        "    name\n"
        "    for name in (\n"
        "        'zeroth.runtime.orchestration.orchestrator',\n"
        "        'zeroth.service',\n"
        "        'zeroth.integrations.persistence.runs',\n"
        "    )\n"
        "    if name in sys.modules\n"
        "]\n"
        "assert not eager, eager\n"
    )

    assert result.returncode == 0, f"light runtime imports are not lazy:\n{result.stderr}"


def test_the_orchestrator_resolves_from_the_canonical_package() -> None:
    """Lazy must not mean absent: the name still resolves on first access."""
    result = _cold(
        "from zeroth.runtime.orchestration import RuntimeOrchestrator\n"
        "assert RuntimeOrchestrator.__name__ == 'RuntimeOrchestrator'\n"
    )

    assert result.returncode == 0, f"canonical orchestrator does not resolve:\n{result.stderr}"


#: Every compatibility suite ZER-25 deleted, paired with the canonical test that
#: now carries the assertion it uniquely owned. The task requires those
#: assertions to be *retained or moved*, never dropped -- so deletion alone is
#: not evidence, and this table is what makes the difference checkable.
RETIRED_SUITES = {
    "tests/langgraph_gateway/test_legacy_surface_parity.py": (
        "tests/langgraph_gateway/test_import_isolation.py",
        "test_canonical_packages_resolve_every_submodule",
    ),
    "tests/langgraph_gateway/test_relocation_boundaries.py": (
        "tests/architecture/test_legacy_surface_removed.py",
        "test_the_ast_scanner_finds_no_file_importing_a_retired_module",
    ),
}


def test_no_legacy_only_test_modules_remain_and_their_assertions_were_kept() -> None:
    """The deleted compatibility suites are gone *and* accounted for.

    A guard that only checked absence would be satisfied by deleting coverage,
    which is the failure this requirement exists to prevent. Each retired suite
    therefore names the canonical test that inherited its assertion, and both
    halves are verified: the old module is gone, and the named replacement
    exists and is collected.
    """
    import ast

    missing_replacements = []
    for retired, (replacement, test_name) in RETIRED_SUITES.items():
        assert not (REPO_ROOT / retired).exists(), f"{retired} still exists"

        path = REPO_ROOT / replacement
        assert path.exists(), f"{replacement} does not exist"
        # The named replacement must exist *and* assert something. Checking only
        # that a function of that name exists would be satisfied by an empty
        # body, which is how deleted coverage disguises itself as moved coverage.
        functions = {
            node.name: node
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        replacement_test = functions.get(test_name)
        if replacement_test is None:
            missing_replacements.append(f"{retired} -> {replacement}::{test_name} (absent)")
        elif not any(isinstance(inner, ast.Assert) for inner in ast.walk(replacement_test)):
            missing_replacements.append(
                f"{retired} -> {replacement}::{test_name} (exists but asserts nothing)"
            )

    assert not missing_replacements, (
        "these retired suites have no surviving replacement assertion:\n  "
        + "\n  ".join(missing_replacements)
    )


#: Documentation trees that must teach canonical imports. ``CHANGELOG.md`` and
#: ``docs/backend-import-migration.md`` are excluded by design: both are
#: historical records whose subject *is* the retired paths, and rewriting them
#: would destroy the evidence a reader migrating off 0.16 needs.
MAINTAINED_DOCS = (
    "README.md",
    "docs/how-to",
    "docs/tutorials",
    "docs/concepts",
    "docs/reference",
)
HISTORICAL_DOCS = ("CHANGELOG.md", "docs/backend-import-migration.md")

#: Prose that must also stop naming retired paths. Docstrings and example
#: scripts are guidance a reader follows, so a stale one is as broken as a stale
#: page -- and the AST scanner cannot see them, because they are not imports.
GUIDANCE_TREES = ("src/zeroth", "examples", "apps/vendor_dd")

#: Two files legitimately keep the old names. ``_architecture.py`` explains, in
#: its exception rationale, what the shim classification used to hide;
#: ``PROVENANCE.md`` records the vendoring event that produced the tree.
GUIDANCE_EXEMPT = (
    "src/zeroth/_architecture.py",
    "src/zeroth/contracts/governed/PROVENANCE.md",
)


def test_docs_use_canonical_imports() -> None:
    """No maintained page may still tell a reader to import a retired module."""
    offenders = []
    for entry in MAINTAINED_DOCS:
        root = REPO_ROOT / entry
        pages = [root] if root.is_file() else sorted(root.rglob("*.md"))
        for page in pages:
            text = page.read_text(encoding="utf-8")
            for root_name in LEGACY_ROOTS:
                if root_name in text:
                    offenders.append(f"{page.relative_to(REPO_ROOT)} mentions {root_name}")

    assert not offenders, (
        "maintained documentation still names a retired module:\n  " + "\n  ".join(offenders)
    )


def test_the_migration_guide_records_the_removal_release() -> None:
    """A reader on a legacy path must be told which release removed it.

    The guide is deliberately exempt from the scan above -- it has to keep
    naming the old paths to be useful -- so it gets its own assertion instead of
    an exemption with nothing behind it.
    """
    guide = (REPO_ROOT / "docs/backend-import-migration.md").read_text(encoding="utf-8")

    assert "removed in 0.17" in guide
    assert "ModuleNotFoundError" in guide
    for root_name in LEGACY_ROOTS:
        assert root_name in guide, f"{root_name} must stay documented for migrating readers"


def test_the_historical_records_are_still_present() -> None:
    """The exemption is a real pair of files, not a hole in the guard."""
    for entry in HISTORICAL_DOCS:
        assert (REPO_ROOT / entry).exists(), entry


def test_docs_use_canonical_imports_in_source_prose_too() -> None:
    """Docstrings and example scripts must not point a reader at a retired path.

    The AST scanner cannot catch these -- they are prose, not imports -- so a
    stale docstring survived the conversion telling readers to run
    ``python -m zeroth.core.service.entrypoint``. This closes that gap.
    """
    offenders = []
    for tree in GUIDANCE_TREES:
        root = REPO_ROOT / tree
        if not root.exists():
            continue
        for page in sorted(list(root.rglob("*.py")) + list(root.rglob("*.md"))):
            if "__pycache__" in page.parts:
                continue
            if page.relative_to(REPO_ROOT).as_posix() in GUIDANCE_EXEMPT:
                continue
            text = page.read_text(encoding="utf-8")
            for root_name in LEGACY_ROOTS:
                if root_name in text:
                    offenders.append(f"{page.relative_to(REPO_ROOT)} mentions {root_name}")

    assert not offenders, "source prose still names a retired module:\n  " + "\n  ".join(offenders)


def test_the_guidance_exemptions_are_real_files_that_still_need_the_old_names() -> None:
    """An exemption with nothing behind it is a hole, not an exemption."""
    for entry in GUIDANCE_EXEMPT:
        path = REPO_ROOT / entry
        assert path.exists(), entry
        text = path.read_text(encoding="utf-8")
        assert any(name in text for name in LEGACY_ROOTS), (
            f"{entry} no longer needs its exemption -- remove it"
        )


#: Assertions that are trivially true and therefore prove nothing. ZER-25's
#: mechanical parity-test conversion produced every shape below, and each one
#: passed a green suite. The first draft of this guard was itself unsound -- it
#: missed keyword-form parametrization, ``x == x``, duplicates separated by a
#: blank line, and assertion-free bodies -- so it is written against sibling
#: statements rather than a line-distance heuristic, and carries no whole-file
#: suppressions: a file-level exemption hides the next real defect in that file.

# A fourth shape -- a test function containing no assertion at all -- is
# deliberately NOT checked here, and the numbers behind that decision are
# reproducible rather than asserted. Run
# ``uv run python scripts/count_assertion_free_tests.py`` to regenerate them:
#
#   * 477 test functions contain no bare ``assert`` statement;
#   * 43 of those remain unexplained after crediting ``pytest.raises``/``fail``/
#     ``warns``, mock ``assert_*`` methods, and helper delegates.
#
# Every one of the 43 sampled asserts for real in a way no static rule sees: a
# bare call that raises on failure, a ``try``/``except`` flow, or a ``_probe``
# helper that asserts inside a subprocess string. Shipping the rule would mean
# 43 suppressions, which is the anti-pattern this guard exists to avoid.


def _is_side_effect_free(node: object) -> bool:
    """Whether an expression invokes nothing that could observe or mutate state.

    Even a builtin call is stateful when its argument is: ``list(iterator)``
    consumes the iterator, and ``len(value)`` may invoke a user-defined
    ``__len__``. The detector therefore stays conservative and accepts false
    negatives rather than rejecting a meaningful test.
    """
    import ast

    return not any(isinstance(inner, ast.Call) for inner in ast.walk(node))


def test_builtin_calls_over_runtime_values_are_not_assumed_side_effect_free() -> None:
    """Builtin calls may consume iterators or invoke user-defined protocols."""
    import ast

    expression = ast.parse("list(values) == list(values)", mode="eval").body

    assert not _is_side_effect_free(expression)


def _empty_parametrization(node: object) -> bool:
    """Whether a ``parametrize`` decorator supplies zero cases, positionally or by keyword."""
    import ast

    if not isinstance(node, ast.Call) or "parametrize" not in ast.unparse(node.func):
        return False
    candidates = list(node.args) + [kw.value for kw in node.keywords if kw.arg == "argvalues"]
    return any(isinstance(item, ast.List | ast.Tuple) and not item.elts for item in candidates)


def _constant_assertion(node: object) -> bool:
    """Whether an assertion's test is a literal, and so has a fixed truth value.

    ``assert True`` cannot fail. Neither can ``assert 1`` or ``assert "reason"``.
    A literal that is *falsy* is a different thing -- ``assert False`` is a
    deliberate unreachable marker and always fails, which is not vacuity -- so
    only the truthy ones are reported.
    """
    import ast

    return (
        isinstance(node, ast.Assert)
        and isinstance(node.test, ast.Constant)
        and bool(node.test.value)
    )


def _self_comparison(node: object) -> bool:
    """Whether an assertion compares a value with itself, in either operator form."""
    import ast
    import re

    if not isinstance(node, ast.Assert):
        return False
    sides = re.match(r"^(.+?) (?:is|==) (.+)$", ast.unparse(node.test))
    return bool(
        sides
        and sides.group(1).strip() == sides.group(2).strip()
        and _is_side_effect_free(node.test)
    )


def test_no_test_is_vacuously_true() -> None:
    """Four shapes, named exactly: this is a ratchet, not a soundness proof.

    It rejects an empty ``parametrize`` (positional or ``argvalues=``), a
    comparison whose two sides must evaluate identically, an assertion repeated
    as the next sibling statement, and an assertion over a truthy literal. It
    does NOT reject a test that asserts nothing -- see the note above
    ``_is_side_effect_free`` for why that rule is absent and how to reproduce the
    number behind that decision.

    The literal rule was missing while ``assert True`` was live in the tree, so
    the guard ran green over exactly the shape it is named for.
    """
    import ast

    problems: list[str] = []
    for path in sorted((REPO_ROOT / "tests").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            if _empty_parametrization(node):
                problems.append(f"{relative}:{node.lineno} parametrizes zero cases")

            if _constant_assertion(node):
                problems.append(f"{relative}:{node.lineno} asserts a literal that cannot fail")

            if _self_comparison(node):
                problems.append(f"{relative}:{node.lineno} compares a value with itself")

            # Consecutive identical assertions. Compared as *sibling statements*,
            # so a blank line between them still counts, while an intervening
            # statement -- the mutate-then-reassert pattern -- correctly does not.
            # Every statement list a node owns -- ``else`` and ``except`` blocks
            # hold sibling statements too, and checking only ``.body`` missed
            # duplicates inside them.
            blocks = [
                block
                for attribute in ("body", "orelse", "finalbody", "handlers")
                if isinstance(block := getattr(node, attribute, None), list)
            ]
            for block in blocks:
                for first, second in zip(block, block[1:], strict=False):
                    if (
                        isinstance(first, ast.Assert)
                        and isinstance(second, ast.Assert)
                        and ast.unparse(first) == ast.unparse(second)
                        and _is_side_effect_free(second)
                    ):
                        problems.append(
                            f"{relative}:{second.lineno} repeats the assertion above it"
                        )

    assert not problems, "these tests pass without proving anything:\n  " + "\n  ".join(problems)
