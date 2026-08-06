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


def test_no_file_imports_a_retired_module() -> None:
    """The flat prohibition: nothing under any scanned tree may import them."""
    offenders = scan_repository()

    assert not offenders, "these files import a retired module:\n  " + "\n  ".join(
        f"{path}:{line} imports {module}"
        for path, hits in sorted(offenders.items())
        for line, module in hits
    )


def test_the_retired_packages_have_no_spec_in_a_fresh_interpreter() -> None:
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


def test_every_backend_domain_imports_without_loading_a_retired_module() -> None:
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


def test_the_scanner_detects_every_import_form(tmp_path: Path) -> None:
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

    assert applied == ["019"]
    assert "runs" in tables


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
