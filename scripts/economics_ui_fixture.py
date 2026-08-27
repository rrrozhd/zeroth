"""Launch the economics fixture after installing its process environment.

This top-level script must remain outside ``release.live_evaluation`` because
that package eagerly imports econ-plane modules whose engine binds at import
time. Installing the environment here prevents any working-directory database.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.economics_ui_fixture_environment import fixture_environment  # noqa: E402


def _option(argv: list[str], name: str, *, default: str | None = None) -> str:
    try:
        value = argv[argv.index(name) + 1]
    except (ValueError, IndexError):
        if default is not None:
            return default
        raise SystemExit(f"{name} is required") from None
    return value


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    state_root = Path(_option(arguments, "--state-root"))
    console_origin = _option(
        arguments,
        "--console-origin",
        default="http://127.0.0.1:3000",
    )
    os.environ.update(
        fixture_environment(state_root, console_origin=console_origin)
    )

    from release.live_evaluation.economics_ui_fixture import main as fixture_main

    return fixture_main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
