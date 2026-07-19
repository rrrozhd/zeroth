"""Data retention governance: the decomposed right-to-erasure surface.

The erasure service is a facade over five collaborators, each owning one
concern:

| Module | Owns |
| --- | --- |
| ``manifests`` | building the cleanup manifest and projecting it into results |
| ``replay`` | folding legacy retention audit entries back into claim state |
| ``claims`` | claim leases, fencing, and the CAS writes behind them |
| ``executor`` | running manifest operations against external surfaces |
| ``compatibility`` | the legacy per-step retention log entries |
| ``errors`` | the two public exception types |

``RetentionErasureService`` itself resolves lazily. Its definition stays in
:mod:`zeroth.core.retention.erasure_service` (a pinned legacy capability), and
that module's body imports this package's collaborators — so resolving it
eagerly here would re-enter a partially initialized module the moment a cold
interpreter starts from either side. ``tests/governance/retention/test_cold_import.py``
pins both directions from subprocesses, because the in-process suite always has
``zeroth.core`` warm and cannot see the cycle.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

from zeroth.governance.retention.claims import CleanupClaims
from zeroth.governance.retention.compatibility import CompatibilityLog, result_detail
from zeroth.governance.retention.errors import LegalHoldError, StaleCleanupClaimError
from zeroth.governance.retention.executor import CleanupExecutor
from zeroth.governance.retention.manifests import (
    build_cleanup_manifest,
    manifest_complete,
    result_from_manifest,
)
from zeroth.governance.retention.replay import CleanupReplayState, replay_cleanup_state

if TYPE_CHECKING:
    from zeroth.governance.retention.service import RetentionErasureService

_EXPORTS = {
    "RetentionErasureService": "zeroth.governance.retention.service",
}

__all__ = [
    "CleanupClaims",
    "CleanupExecutor",
    "CleanupReplayState",
    "CompatibilityLog",
    "LegalHoldError",
    "RetentionErasureService",
    "StaleCleanupClaimError",
    "build_cleanup_manifest",
    "manifest_complete",
    "replay_cleanup_state",
    "result_detail",
    "result_from_manifest",
]


def __getattr__(name: str) -> object:
    """Resolve the erasure service from its module on first access."""
    module = _EXPORTS.get(name)
    if module is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(__all__) | set(_EXPORTS))
