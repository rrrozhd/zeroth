"""Canonical import surface for the console UI mount module.

Named test_console_ui_surface.py rather than the plan's
test_console_ui.py: a root-level tests/test_console_ui.py already
exists, and two test modules with one basename cannot both be collected
under namespace packages.
"""

from __future__ import annotations


def test_console_ui_is_the_same_surface_through_both_paths() -> None:
    from zeroth.core.service import console_ui as legacy
    from zeroth.service.api import console_ui as canonical

    assert canonical.mount_console is legacy.mount_console
    assert canonical.console_cors_origins is legacy.console_cors_origins
