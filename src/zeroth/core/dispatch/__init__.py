"""Legacy import path for durable run dispatch.

Lease claiming and ARQ wakeup plumbing live in
:mod:`zeroth.platform.dispatch`; the run worker lives in
:mod:`zeroth.runtime.orchestration.run_worker` and is republished lazily so
that importing this package does not load the runtime layer. Import from the
canonical locations instead (see docs/backend-import-migration.md).
"""

from typing import TYPE_CHECKING, Any

from zeroth.platform.dispatch.lease import LeaseManager

if TYPE_CHECKING:
    from zeroth.runtime.orchestration.run_worker import RunWorker

__all__ = ["LeaseManager", "RunWorker"]

try:
    from zeroth.platform.dispatch.arq_wakeup import (
        WAKEUP_TASK_NAME,
        arq_settings_from_zeroth,
        create_arq_pool,
        enqueue_wakeup,
        run_arq_consumer,
    )

    __all__ += [
        "WAKEUP_TASK_NAME",
        "arq_settings_from_zeroth",
        "create_arq_pool",
        "enqueue_wakeup",
        "run_arq_consumer",
    ]
except ImportError:
    pass


def __getattr__(name: str) -> Any:
    """Lazily republish the run worker from the runtime layer."""
    if name == "RunWorker":
        from zeroth.runtime.orchestration.run_worker import RunWorker

        return RunWorker
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
