"""Build step for the finmetrics project unit.

Converts the raw ``financials.csv`` into the ``data/financials.json`` artifact
``main.py`` reads at run time. This is the PROJECT-mode build hook: the
runtime executes it (via ``BuildConfig.command``) before the unit's first run.
Idempotent by construction.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> None:
    rows_by_vendor: dict[str, list[dict[str, float | str]]] = {}
    with (HERE / "financials.csv").open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            slug = row["vendor_slug"]
            rows_by_vendor.setdefault(slug, []).append(
                {
                    "quarter": row["quarter"],
                    "revenue_usd": float(row["revenue_usd"]),
                    "net_income_usd": float(row["net_income_usd"]),
                    "current_assets_usd": float(row["current_assets_usd"]),
                    "current_liabilities_usd": float(row["current_liabilities_usd"]),
                }
            )
    out_dir = HERE / "data"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "financials.json").write_text(
        json.dumps(rows_by_vendor, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(f"finmetrics build: wrote {len(rows_by_vendor)} vendors")


if __name__ == "__main__":
    main()
