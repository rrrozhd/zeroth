"""Legacy import path for the platform observability package.

Observability lives in :mod:`zeroth.platform.observability`; this package
republishes the same objects for compatibility. Import from the canonical
location instead (see docs/backend-import-migration.md).
"""

from zeroth.platform.observability import (
    MetricsCollector,
    configure_tracing,
    get_correlation_id,
    new_correlation_id,
    set_correlation_id,
    start_span,
)

__all__ = [
    "MetricsCollector",
    "configure_tracing",
    "get_correlation_id",
    "new_correlation_id",
    "set_correlation_id",
    "start_span",
]
