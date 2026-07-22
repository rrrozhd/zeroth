"""Legacy import location for the deployment api module.

The definitions now live in :mod:`zeroth.service.api.deployment_api`. This module republishes
the same objects, so the protected legacy import path keeps resolving to
identical types and functions.
"""

from __future__ import annotations

from zeroth.service.api.deployment_api import CreateDeploymentRequest as CreateDeploymentRequest
from zeroth.service.api.deployment_api import DeploymentSummaryResponse as DeploymentSummaryResponse
from zeroth.service.api.deployment_api import RollbackDeploymentRequest as RollbackDeploymentRequest
from zeroth.service.api.deployment_api import _summary as _summary
from zeroth.service.api.deployment_api import (
    register_deployment_routes as register_deployment_routes,
)
