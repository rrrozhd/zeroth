"""Ratchet the retired ``zeroth.core`` / ``zeroth.econ_plane`` import surface to zero.

ZER-25 removes both trees outright. The removal touches roughly a sixth of the
repository's Python files, so the conversion runs as a ratchet rather than one
commit: ``legacy_import_baseline.txt`` lists every file that still imports a
retired module, and the two guards below let that list shrink but never grow and
never go stale.

The baseline is scaffolding. When it reaches zero the trees are deleted and this
module keeps only the flat prohibition -- no allowlist, no exemptions -- which is
the end state the task specifies. Assertions that describe a relocation land in
the commit that performs it, not here, so every commit in the sequence is green
on its own terms.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tests.architecture.legacy_imports import (
    REPO_ROOT,
    is_legacy_module,
    legacy_imports_in,
    scan_repository,
)

BASELINE_PATH = Path(__file__).with_name("legacy_import_baseline.txt")


def _cold(code: str) -> subprocess.CompletedProcess[str]:
    """Run ``code`` in a fresh interpreter.

    In-process assertions cannot answer import questions here: ``conftest``
    imports service bootstrap at collection time, so ``zeroth.core`` and most of
    the service graph are already in ``sys.modules`` by the time any test body
    runs. Only a cold interpreter sees what a library consumer sees.
    """
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)


def test_alembic_resolves_migrations_only_from_the_service_domain() -> None:
    """No configuration may still point Alembic at the retired package.

    A stale ``script_location`` fails at deploy time, not at import time, so a
    green suite is not evidence on its own -- the paths are asserted directly.
    """
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
    """The orchestrator stays lazy behind the runtime package surface.

    Persistence adapters import the narrow runtime protocols. If reaching them
    dragged in the orchestrator, it would also drag in the service and the
    persistence adapters it drives -- the cycle the lazy package init exists to
    prevent.
    """
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


def test_the_scanner_detects_every_import_form(tmp_path: Path) -> None:
    """A guard that cannot see an offender would pass vacuously forever.

    Covers the four shapes the repository actually uses, plus the two kinds of
    false positive that make a substring search useless here: prose naming the
    old path, and a canonical module whose name merely starts the same way.
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
    assert is_legacy_module("zeroth.core")
    assert is_legacy_module("zeroth.core.runs.models")
    assert is_legacy_module("zeroth.econ_plane")
    assert not is_legacy_module("zeroth.econ")
    assert not is_legacy_module("zeroth.econ.plane")
    assert not is_legacy_module("zeroth.corex")
    assert not is_legacy_module(None)


def _baseline() -> set[str]:
    lines = BASELINE_PATH.read_text(encoding="utf-8").splitlines()
    return {line.strip() for line in lines if line.strip() and not line.startswith("#")}


def test_no_file_outside_the_baseline_imports_a_retired_module() -> None:
    """New legacy imports are rejected; the conversion may only move forward."""
    offenders = set(scan_repository())
    added = sorted(offenders - _baseline())

    assert not added, (
        "these files import zeroth.core or zeroth.econ_plane and are not in the "
        "ZER-25 baseline -- import the canonical package instead:\n  " + "\n  ".join(added)
    )


def test_the_baseline_lists_no_file_that_is_already_converted() -> None:
    """A converted file must leave the baseline in the same commit that converts it.

    Without this the list would decay into a permanent exemption roster: entries
    for files that no longer offend would sit there indefinitely, and the guard
    above would silently stop protecting them.
    """
    offenders = set(scan_repository())
    stale = sorted(entry for entry in _baseline() if entry not in offenders)

    assert not stale, (
        "these files no longer import a retired module but are still listed in "
        f"{BASELINE_PATH.name} -- delete their lines:\n  " + "\n  ".join(stale)
    )


def test_every_baseline_entry_names_a_file_that_exists() -> None:
    """A deleted file leaves the baseline too, so the list stays a live inventory."""
    missing = sorted(entry for entry in _baseline() if not (REPO_ROOT / entry).exists())

    assert not missing, f"{BASELINE_PATH.name} names files that no longer exist:\n  " + "\n  ".join(
        missing
    )
