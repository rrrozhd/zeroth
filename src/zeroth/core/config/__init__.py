"""Legacy import path for the platform config package.

The configuration system lives in :mod:`zeroth.platform.config`; this package
republishes the same objects for compatibility. Import from the canonical
location instead (see docs/backend-import-migration.md).
"""

from zeroth.platform.config import ZerothSettings, get_settings

__all__ = ["ZerothSettings", "get_settings"]
