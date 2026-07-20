"""Governed memory vocabulary (vendored from governai 0.2.3).

``MemoryScope`` is authored-binding and connector vocabulary consumed across
the runtime, integrations, and service domains, so its definition lives with
the governed contracts. The ``MemoryEntry`` model and the connector
implementations remain in :mod:`zeroth.core.governed.memory` until the memory
integrations consolidate.
"""

from __future__ import annotations

from enum import Enum


class MemoryScope(str, Enum):
    RUN = "run"
    THREAD = "thread"
    SHARED = "shared"
