"""Fail closed unless the Zeroth readiness payload is exactly healthy."""

from __future__ import annotations

import json
import sys
from urllib.request import urlopen


def main() -> int:
    """Return success only for a parsed readiness status of ``ok``."""
    try:
        with urlopen("http://127.0.0.1:8000/health/ready", timeout=3) as response:
            payload = json.load(response)
    except Exception as error:  # noqa: BLE001 - health probes must fail closed
        print(f"readiness request failed: {error}", file=sys.stderr)
        return 1
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        print(f"readiness status is {payload!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
