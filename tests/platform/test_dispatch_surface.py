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

import pytest


def test_dispatch_is_the_same_surface_through_both_paths() -> None:
    from zeroth.core import dispatch as legacy
    from zeroth.platform import dispatch as canonical

    assert canonical.LeaseManager is legacy.LeaseManager
    assert canonical.WAKEUP_TASK_NAME is legacy.WAKEUP_TASK_NAME
    assert canonical.arq_settings_from_zeroth is legacy.arq_settings_from_zeroth
    assert canonical.create_arq_pool is legacy.create_arq_pool
    assert canonical.enqueue_wakeup is legacy.enqueue_wakeup
    assert canonical.run_arq_consumer is legacy.run_arq_consumer


def test_the_run_worker_lives_in_the_runtime_domain() -> None:
    """RunWorker composes lease claiming with orchestration, so it is runtime.

    It drives ``RuntimeOrchestrator``, transitions run models, and consults the
    approval service; none of that may sit below the runtime layer. The legacy
    ``zeroth.core.dispatch`` path keeps republishing it lazily.
    """
    from zeroth.core import dispatch as legacy
    from zeroth.runtime.orchestration import run_worker

    assert legacy.RunWorker is run_worker.RunWorker


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("zeroth.platform.dispatch", "zeroth.core.dispatch"),
        ("zeroth.core.dispatch", "zeroth.platform.dispatch"),
    ],
)
def test_dispatch_cold_imports_from_both_directions(first: str, second: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}\nimport {second}\n"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"cold import {first} then {second} failed:\n{result.stderr}"
