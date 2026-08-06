"""Legacy import path for the ``zeroth-core`` command-line interface.

It now lives in :mod:`zeroth.service.cli`; this module republishes exactly the
names it published before ZER-25 relocated it. Import from the canonical
location instead (see docs/backend-import-migration.md).
"""

from __future__ import annotations

from zeroth.service.cli import build_parser, ensure_schema, main

__all__ = ["build_parser", "ensure_schema", "main"]
