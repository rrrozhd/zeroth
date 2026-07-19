"""Econ-event erasure hook (WS-E).

The economic control plane (``zeroth.econ_plane``) records ``execution_events``
and ``outcome_events`` that carry tenant / cost / potentially-PII payloads on its
OWN SQLAlchemy database — separate from the core append-only audit DB. Right-to-
erasure must reach that data too, so the erasure service calls this interface.

Correlation caveat (the named deferred item — see docs/retention-and-erasure.md):
econ events are keyed by ``join_key``, a *business-request* identifier resolved
from runtime context, and there is NO durable ``run_id -> join_key`` index. The
erasure service therefore passes the run id plus the explicit top-level economic
``join_key`` from audit ``execution_metadata``. Nested payload keys are never
trusted as correlation authority. Every deletion is constrained by both tenant
and join key; automatic, complete run->join_key resolution remains deferred.

Only the protocol lives here. The concrete SQLAlchemy adapter moved to
:mod:`zeroth.econ.plane.erasure` — it is econ-plane code, and its presence in
this module was the only reason the governance domain imported
``zeroth.econ_plane``. The re-export below resolves lazily so this module keeps
no import edge into the econ domain.
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

_EXPORTS = {
    "SqlAlchemyEconEventEraser": "zeroth.econ.plane.erasure",
}


@runtime_checkable
class EconEventEraser(Protocol):
    """Deletes one tenant's econ execution/outcome events for given join keys."""

    async def delete_events_for_run(
        self,
        tenant_id: str,
        join_keys: Sequence[str],
        *,
        idempotency_key: str,
    ) -> int:
        """Delete tenant events whose ``join_key`` is in ``join_keys``.

        Returns the total number of rows deleted across event tables.
        """
        ...


def __getattr__(name: str) -> object:
    """Resolve the concrete econ-plane adapter from its own domain on first access."""
    module = _EXPORTS.get(name)
    if module is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({"EconEventEraser", *_EXPORTS})
