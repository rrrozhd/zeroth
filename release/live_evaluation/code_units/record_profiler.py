"""Standalone JSON-stdin record profiler used by the local evaluation manifest."""

from __future__ import annotations

import json
import sys


def _missing(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def main() -> int:
    payload = json.load(sys.stdin)
    records = payload["records"]
    required_fields = payload["required_fields"]
    missing_counts = {
        field: sum(1 for record in records if _missing(record.get(field)))
        for field in required_fields
    }
    complete_records = sum(
        1
        for record in records
        if all(not _missing(record.get(field)) for field in required_fields)
    )
    total_records = len(records)
    json.dump(
        {
            "total_records": total_records,
            "missing_counts": missing_counts,
            "complete_records": complete_records,
            "completeness_pct": round(complete_records / total_records * 100, 2),
            "ready": complete_records == total_records,
        },
        sys.stdout,
        sort_keys=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
