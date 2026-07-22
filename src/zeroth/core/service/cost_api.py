"""Legacy import location for the cost api module.

The definitions now live in :mod:`zeroth.service.api.cost_api`. This module republishes
the same objects, so the protected legacy import path keeps resolving to
identical types and functions.
"""

from __future__ import annotations

from zeroth.service.api.cost_api import DeploymentCostResponse as DeploymentCostResponse
from zeroth.service.api.cost_api import TenantBudgetRequest as TenantBudgetRequest
from zeroth.service.api.cost_api import TenantCostResponse as TenantCostResponse
from zeroth.service.api.cost_api import _regulus_self_auth_headers as _regulus_self_auth_headers
from zeroth.service.api.cost_api import register_cost_routes as register_cost_routes
