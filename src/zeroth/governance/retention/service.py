"""Canonical import location for the retention erasure service.

The class definition stays in :mod:`zeroth.core.retention.erasure_service` and
is republished here, not relocated. That is a deliberate constraint of the
protected surface: ``RetentionErasureService`` is a pinned legacy capability,
and the facade keeps every private helper the existing suite drives directly.
The service itself is a thin composition over the collaborators in this
package — manifests, replay, claims, executor, compatibility — each of which it
rebuilds per access from its injected repositories.

This module is resolved lazily by the package ``__init__``: importing it
executes the legacy module, whose body imports this package's collaborators, so
an eager import here would re-enter a partially initialized module in a cold
interpreter.
"""

from __future__ import annotations

from zeroth.core.retention.erasure_service import (
    RetentionErasureService as RetentionErasureService,
)
