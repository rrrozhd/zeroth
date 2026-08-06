"""Canonical import surface for the platform dispatch package.

Non-golden boundary tests for the Task 11 dispatch move: lease management and
ARQ wakeup plumbing are platform infrastructure and live in
``zeroth.platform.dispatch``; the run worker drives the orchestrator and lives
in the runtime domain. The legacy ``zeroth.core.dispatch`` path keeps
republishing all of it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_dispatch_publishes_its_whole_surface() -> None:
    """Every name the package is documented to export is still exported.

    This replaced a parity assertion comparing each name against the legacy
    republisher. ZER-25 removed that path, so the comparison would compare
    the module with itself; the surface it pinned is asserted directly.
    """
    import zeroth.platform.dispatch as canonical

    expected = {
        "LeaseManager",
        "WAKEUP_TASK_NAME",
        "arq_settings_from_zeroth",
        "create_arq_pool",
        "enqueue_wakeup",
        "run_arq_consumer",
    }

    missing = sorted(name for name in expected if not hasattr(canonical, name))
    assert not missing, f"zeroth.platform.dispatch no longer publishes: {missing}"


def test_the_run_worker_lives_in_the_runtime_domain() -> None:
    """RunWorker composes lease claiming with orchestration, so it is runtime.

    It drives ``RuntimeOrchestrator``, transitions run models, and consults the
    approval service; none of that may sit below the runtime layer. The legacy
    ``zeroth.core.dispatch`` path keeps republishing it lazily.
    """
    from zeroth.runtime.orchestration import run_worker

    assert hasattr(run_worker, "RunWorker")


def test_dispatch_imports_in_a_cold_interpreter() -> None:
    """The canonical package imports with nothing else pre-warmed.

    This kept the canonical half of a test that used to import the legacy
    and canonical packages in both orders, guarding a cycle between them.
    With the legacy package gone there is one direction left to guard.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import zeroth.platform.dispatch"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_dispatch_extra_smoke_uses_the_canonical_packaged_surface() -> None:
    """The wheel gate must follow dispatch moves and exercise its dependencies."""
    workflow = (REPO_ROOT / ".github/workflows/verify-extras.yml").read_text(encoding="utf-8")

    assert "zeroth.core.dispatch.worker" not in workflow
    assert workflow.count("import arq, redis, zeroth.platform.dispatch.arq_wakeup") == 2
