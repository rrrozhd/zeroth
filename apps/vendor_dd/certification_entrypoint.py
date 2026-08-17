"""Seed vendor-dd and start its service inside the verified runtime process."""

from __future__ import annotations

import asyncio
import os
import sys
from importlib import import_module


def main() -> int:
    """Require certification auth, seed once, then run the HTTP service in-process."""
    api_key = os.environ.get("APP_CERTIFICATION_API_KEY")
    if not api_key:
        print("APP_CERTIFICATION_API_KEY is required", file=sys.stderr)
        return 2
    os.environ["VENDOR_DD_API_KEY"] = api_key
    seeded = asyncio.run(import_module("apps.vendor_dd.seed").main())
    if seeded:
        return seeded
    return import_module("apps.vendor_dd.entrypoint").main()


if __name__ == "__main__":
    raise SystemExit(main())
