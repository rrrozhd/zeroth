"""Legacy import location for the artifact api module.

The definitions now live in :mod:`zeroth.service.api.artifact_api`. This module republishes
the same objects, so the protected legacy import path keeps resolving to
identical types and functions.
"""

from __future__ import annotations

from zeroth.service.api.artifact_api import _artifact_store as _artifact_store
from zeroth.service.api.artifact_api import register_artifact_routes as register_artifact_routes
