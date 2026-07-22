"""Legacy import location for the admin api module.

The definitions now live in :mod:`zeroth.service.api.admin_api`. This module republishes
the same objects, so the protected legacy import path keeps resolving to
identical types and functions.
"""

from __future__ import annotations

from zeroth.service.api.admin_api import AdminRunListResponse as AdminRunListResponse
from zeroth.service.api.admin_api import _bootstrap as _bootstrap
from zeroth.service.api.admin_api import register_admin_routes as register_admin_routes
