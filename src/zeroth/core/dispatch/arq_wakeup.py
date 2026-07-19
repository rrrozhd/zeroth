"""Legacy import path for :mod:`zeroth.platform.dispatch.arq_wakeup`."""

from zeroth.platform.dispatch.arq_wakeup import (
    WAKEUP_TASK_NAME,
    arq_settings_from_zeroth,
    create_arq_pool,
    enqueue_wakeup,
    run_arq_consumer,
)

__all__ = [
    "WAKEUP_TASK_NAME",
    "arq_settings_from_zeroth",
    "create_arq_pool",
    "enqueue_wakeup",
    "run_arq_consumer",
]
