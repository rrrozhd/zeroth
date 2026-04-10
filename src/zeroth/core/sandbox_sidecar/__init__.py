"""Zeroth sandbox sidecar service package.

The sidecar holds the Docker socket and exposes a REST API for sandboxed
execution. The main API container communicates with it over HTTP.
"""

from zeroth.core.sandbox_sidecar.app import app

__all__ = ["app"]
