"""Legacy import path for the sandbox integrations package.

The sandbox sidecar service lives in :mod:`zeroth.integrations.sandbox`;
this package republishes the same objects for compatibility. Import from
the canonical location instead (see docs/backend-import-migration.md).
"""

from zeroth.integrations.sandbox.app import app

__all__ = ["app"]
