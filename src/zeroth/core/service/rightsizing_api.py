"""Legacy import location for the rightsizing api module.

The definitions now live in :mod:`zeroth.service.api.rightsizing_api`. This module republishes
the same objects, so the protected legacy import path keeps resolving to
identical types and functions.
"""

from __future__ import annotations

from zeroth.service.api.rightsizing_api import ExperimentRequest as ExperimentRequest
from zeroth.service.api.rightsizing_api import RightsizingRequest as RightsizingRequest
from zeroth.service.api.rightsizing_api import (
    register_rightsizing_routes as register_rightsizing_routes,
)
