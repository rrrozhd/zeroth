"""Legacy import location for the service process entry point.

The implementation now lives in :mod:`zeroth.service.entrypoint`. This module
republishes the same objects, so the protected legacy import path keeps
resolving to identical objects.
"""

from __future__ import annotations

from zeroth.service.entrypoint import _bootstrap as _bootstrap
from zeroth.service.entrypoint import _serve as _serve
from zeroth.service.entrypoint import app_factory as app_factory
from zeroth.service.entrypoint import logger as logger
from zeroth.service.entrypoint import main as main

if __name__ == "__main__":
    main()
