"""Legacy import location for the console ui module.

The definitions now live in :mod:`zeroth.service.api.console_ui`. This module republishes
the same objects, so the protected legacy import path keeps resolving to
identical types and functions.
"""

from __future__ import annotations

from zeroth.service.api.console_ui import _ENV_CORS as _ENV_CORS
from zeroth.service.api.console_ui import _ENV_DIR as _ENV_DIR
from zeroth.service.api.console_ui import CONSOLE_MOUNT_PATH as CONSOLE_MOUNT_PATH
from zeroth.service.api.console_ui import console_cors_origins as console_cors_origins
from zeroth.service.api.console_ui import find_console_dir as find_console_dir
from zeroth.service.api.console_ui import logger as logger
from zeroth.service.api.console_ui import mount_console as mount_console
