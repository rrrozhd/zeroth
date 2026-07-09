"""Entry point of the finmetrics PROJECT unit.

Reads the run payload from ``ZEROTH_INPUT_FILE`` (JSON), looks the vendor up
in the built ``data/financials.json`` artifact, computes solvency/trend
metrics, and writes the payload echoed back plus ``financial_metrics`` to
``ZEROTH_OUTPUT_FILE``.

Runs sandboxed (``python3 -I``); it must only use the standard library.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent


def compute_metrics(rows: list[dict]) -> dict:
    rows = sorted(rows, key=lambda r: r["quarter"])
    first, last = rows[0], rows[-1]
    revenue_trend_pct = (
        ((last["revenue_usd"] - first["revenue_usd"]) / first["revenue_usd"]) * 100.0
        if first["revenue_usd"]
        else 0.0
    )
    margins = [(r["net_income_usd"] / r["revenue_usd"]) * 100.0 for r in rows if r["revenue_usd"]]
    avg_net_margin_pct = sum(margins) / len(margins) if margins else 0.0
    current_ratio = (
        last["current_assets_usd"] / last["current_liabilities_usd"]
        if last["current_liabilities_usd"]
        else 0.0
    )
    going_concern_flag = current_ratio < 1.0 or (avg_net_margin_pct < 0 and revenue_trend_pct < 0)
    return {
        "quarters_analyzed": len(rows),
        "revenue_trend_pct": round(revenue_trend_pct, 2),
        "avg_net_margin_pct": round(avg_net_margin_pct, 2),
        "current_ratio": round(current_ratio, 2),
        "going_concern_flag": going_concern_flag,
    }


def main() -> None:
    payload = json.loads(Path(os.environ["ZEROTH_INPUT_FILE"]).read_text(encoding="utf-8"))
    dataset = json.loads((HERE / "data" / "financials.json").read_text(encoding="utf-8"))
    slug = payload.get("vendor_slug", "")
    rows = dataset.get(slug, [])
    output = dict(payload)
    if rows:
        output["financial_metrics"] = compute_metrics(rows)
        output["financials_found"] = True
    else:
        output["financial_metrics"] = {
            "quarters_analyzed": 0,
            "revenue_trend_pct": 0.0,
            "avg_net_margin_pct": 0.0,
            "current_ratio": 0.0,
            "going_concern_flag": False,
        }
        output["financials_found"] = False
    Path(os.environ["ZEROTH_OUTPUT_FILE"]).write_text(json.dumps(output), encoding="utf-8")


if __name__ == "__main__":
    main()
