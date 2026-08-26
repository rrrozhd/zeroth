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
    host = os.environ.get("HOST", "127.0.0.1")
    connect_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    base_url = f"http://{connect_host}:{os.environ.get('PORT', '8000')}/regulus/v1"
    tenant = os.environ.get("VENDOR_DD_TENANT", "tenant-acme")
    os.environ.setdefault("ZEROTH_REGULUS__BASE_URL", base_url)
    os.environ.setdefault("ECP_BASE_URL", base_url)
    os.environ.setdefault("ECP_SERVICE_PRINCIPAL_TENANT_ID", tenant)
    os.environ["VENDOR_DD_API_KEY"] = api_key
    seeded = asyncio.run(import_module("apps.vendor_dd.seed").main())
    if seeded:
        return seeded
    return import_module("apps.vendor_dd.entrypoint").main()


if __name__ == "__main__":
    raise SystemExit(main())
