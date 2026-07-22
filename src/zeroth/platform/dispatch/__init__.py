"""Durable run dispatch infrastructure: lease claiming and ARQ wakeups.

``LeaseManager`` owns the SQL lease protocol over the runs table; the ARQ
helpers provide best-effort wakeup signals so workers can skip a poll
interval. The run worker that drives claimed runs through the orchestrator is
runtime code and lives in :mod:`zeroth.runtime.orchestration.run_worker`.
"""

from zeroth.platform.dispatch.lease import LeaseManager

__all__ = ["LeaseManager"]

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
