"""Canonical import surface for the console UI mount module.

Named test_console_ui_surface.py rather than the plan's
test_console_ui.py: a root-level tests/test_console_ui.py already
exists, and two test modules with one basename cannot both be collected
under namespace packages.
"""

from __future__ import annotations


def test_console_ui_publishes_its_whole_surface() -> None:
    """Every name the package is documented to export is still exported.

    This replaced a parity assertion comparing each name against the legacy
    republisher. ZER-25 removed that path, so the comparison would compare
    the module with itself; the surface it pinned is asserted directly.
    """
    import zeroth.service.api.console_ui as canonical

    expected = {
        "console_cors_origins",
        "mount_console",
    }

    missing = sorted(name for name in expected if not hasattr(canonical, name))
    assert not missing, f"zeroth.service.api.console_ui no longer publishes: {missing}"
