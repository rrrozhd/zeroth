"""Compatibility module: the erasure facade lives in ``erasure_service``.

Kept because consumers and the canonical package's earlier layout imported
:class:`RetentionErasureService` from this module path.
"""

from __future__ import annotations

from zeroth.governance.retention.erasure_service import (
    RetentionErasureService as RetentionErasureService,
)
