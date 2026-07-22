"""Legacy import location for the health module.

The definitions now live in :mod:`zeroth.service.api.health`. This module republishes
the same objects, so the protected legacy import path keeps resolving to
identical types and functions.
"""

from __future__ import annotations

from zeroth.service.api.health import DependencyStatus as DependencyStatus
from zeroth.service.api.health import LivenessResponse as LivenessResponse
from zeroth.service.api.health import ReadinessResponse as ReadinessResponse
from zeroth.service.api.health import _unavailable as _unavailable
from zeroth.service.api.health import check_database as check_database
from zeroth.service.api.health import check_redis as check_redis
from zeroth.service.api.health import check_regulus as check_regulus
from zeroth.service.api.health import determine_readiness_status as determine_readiness_status
from zeroth.service.api.health import register_health_routes as register_health_routes
