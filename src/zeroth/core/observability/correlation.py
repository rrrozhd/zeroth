"""Legacy import path for :mod:`zeroth.platform.observability.correlation`."""

from zeroth.platform.observability.correlation import (
    get_correlation_id,
    new_correlation_id,
    set_correlation_id,
)

__all__ = [
    "get_correlation_id",
    "new_correlation_id",
    "set_correlation_id",
]
