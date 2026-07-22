"""Legacy import location for the econ analytics api module.

The definitions now live in :mod:`zeroth.service.api.econ_analytics_api`. This module republishes
the same objects, so the protected legacy import path keeps resolving to
identical types and functions.
"""

from __future__ import annotations

from zeroth.service.api.econ_analytics_api import QualityVerdictRequest as QualityVerdictRequest
from zeroth.service.api.econ_analytics_api import (
    _windowed_runs_and_audits as _windowed_runs_and_audits,
)
from zeroth.service.api.econ_analytics_api import (
    register_econ_analytics_routes as register_econ_analytics_routes,
)
