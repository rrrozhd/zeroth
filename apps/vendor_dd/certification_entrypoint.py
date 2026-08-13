"""Seed vendor-dd and replace this process with its service."""

from __future__ import annotations

import os
import subprocess
import sys


def main() -> int:
    """Require certification auth, seed once, then exec the HTTP service."""
    api_key = os.environ.get("APP_CERTIFICATION_API_KEY")
    if not api_key:
        print("APP_CERTIFICATION_API_KEY is required", file=sys.stderr)
        return 2
    os.environ["VENDOR_DD_API_KEY"] = api_key
    seeded = subprocess.run(
        [sys.executable, "-m", "apps.vendor_dd.seed"],
        check=False,
        shell=False,
    )
    if seeded.returncode:
        return seeded.returncode
    os.execv(
        sys.executable,
        [sys.executable, "-m", "apps.vendor_dd.entrypoint"],
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
