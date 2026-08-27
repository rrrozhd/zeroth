"""Provider-neutral protocols implemented by Check framework adapters."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Protocol


class CheckTarget(Protocol):
    """A rebuildable target with caller-owned durable checkpoint storage."""

    graph_factory: Callable[[Any], Any]
    checkpointer_factory: Callable[[Path], AbstractContextManager[Any]]
    entrypoint_digest: str

    def invoke(
        self,
        *,
        case: str,
        scenario_run_id: str,
        checkpointer_path: str | Path,
    ) -> Mapping[str, Any]: ...
