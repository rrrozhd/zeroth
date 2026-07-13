"""Vendored subset of the ``governai`` framework (absorbed from PyPI ``governai==0.2.3``).

Zeroth previously depended on ``governai`` as an external package but used only a
curated slice of it — the memory type system and connector wrappers, the run-state
model, tool primitives, tool-call helpers, the flow/step spec types, and the audit
emitters. That slice was vendored here so zeroth owns it directly; governai's
execution kernel (``runtime/local``, ``workflows``, ``approvals``, ``policies``,
``agents``, ``sandbox``, ``skills``) was intentionally left behind — zeroth runs its
own orchestrator. See ``PROVENANCE.md`` for the file inventory and rationale.

This top-level namespace re-exports only the three names zeroth imported as
``from governai import ...``; everything else is imported from its submodule
(``zeroth.core.governed.memory.models``, ``.tools.base``, ``.app.spec``, …).
"""

from __future__ import annotations

from zeroth.core.governed.models.common import RunStatus
from zeroth.core.governed.models.run_state import RunState
from zeroth.core.governed.tools.base import Tool

__all__ = ["RunState", "RunStatus", "Tool"]
