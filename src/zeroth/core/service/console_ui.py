"""Static-file mount for the optional Zeroth Console front-end.

The console is a Next.js *static export*. When its build output is present we
mount it at ``/console`` so a single Zeroth deployment serves both the API and
its UI on one origin (no separate host, no CORS needed). When absent — e.g. a
Python-only install or CI box with no Node toolchain — the mount is skipped
silently and the API is unaffected.

Standalone hosting (UI on a different origin than the API) is supported via
:func:`console_cors_origins`, consumed by ``create_app``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger(__name__)

CONSOLE_MOUNT_PATH = "/console"
_ENV_DIR = "ZEROTH_CONSOLE_DIR"
_ENV_CORS = "ZEROTH_CONSOLE_CORS_ORIGINS"


def find_console_dir() -> Path | None:
    """Locate the built console (Next static-export ``out/`` dir), or ``None``.

    Resolution order:
      1. ``$ZEROTH_CONSOLE_DIR`` — explicit override (any deployment layout).
      2. ``frontend/out`` relative to the repo root — source checkout / dev,
         where a fresh local build should win over an installed package.
      3. The optional ``zeroth-console`` package — the ``[console]`` extra
         (``pip install "zeroth-core[console]"``), for Python-only installs.
    A directory only counts if it contains an ``index.html``.
    """
    override = os.environ.get(_ENV_DIR)
    if override:
        path = Path(override).expanduser()
        return path if (path / "index.html").is_file() else None

    # src/zeroth/core/service/console_ui.py -> repo root is 4 parents up.
    repo_root = Path(__file__).resolve().parents[4]
    candidate = repo_root / "frontend" / "out"
    if (candidate / "index.html").is_file():
        return candidate

    try:
        from zeroth_console import console_dir
    except ImportError:
        return None
    packaged = console_dir()
    return packaged if (packaged / "index.html").is_file() else None


def console_cors_origins() -> list[str]:
    """Allowed origins for standalone console hosting (comma-separated env)."""
    raw = os.environ.get(_ENV_CORS, "").strip()
    if not raw:
        return []
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def mount_console(app: FastAPI, *, directory: Path | None = None) -> bool:
    """Mount the console static export at ``/console`` if present.

    Returns ``True`` if mounted, ``False`` if no build was found.
    """
    directory = directory if directory is not None else find_console_dir()
    if directory is None:
        logger.info(
            "console UI not mounted: no build found (set %s, or run `npm run build` in frontend/)",
            _ENV_DIR,
        )
        return False

    # Redirect the bare mount path to its trailing-slash form so the
    # StaticFiles(html=True) directory index resolves.
    @app.get(CONSOLE_MOUNT_PATH, include_in_schema=False)
    async def _console_redirect() -> RedirectResponse:
        return RedirectResponse(url=f"{CONSOLE_MOUNT_PATH}/")

    app.mount(
        CONSOLE_MOUNT_PATH,
        StaticFiles(directory=str(directory), html=True),
        name="console",
    )
    logger.info("console UI mounted at %s/ from %s", CONSOLE_MOUNT_PATH, directory)
    return True
