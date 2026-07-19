"""Legacy import location for the manifest api module.

The definitions now live in :mod:`zeroth.service.api.manifest_api`. This module republishes
the same objects, so the protected legacy import path keeps resolving to
identical types and functions.
"""

from __future__ import annotations

from zeroth.service.api.manifest_api import ManifestSummaryResponse as ManifestSummaryResponse
from zeroth.service.api.manifest_api import register_manifest_routes as register_manifest_routes
