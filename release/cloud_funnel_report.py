"""Aggregate self-serve conversion evidence without customer identifiers."""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import tempfile
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from zeroth.econ.plane.backtesting.models import EconomicBacktestRecord
from zeroth.econ.plane.cloud.models import CloudSubscription, CloudTenantBinding


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator * 100 / denominator, 2) if denominator else None


def _percentile_nearest_rank(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 2)


def build_funnel_report(
    session: Session,
    *,
    as_of: datetime,
    window_days: int,
) -> dict[str, object]:
    if as_of.tzinfo is None:
        raise ValueError("as_of must include a timezone")
    if window_days < 1 or window_days > 3650:
        raise ValueError("window_days must be between 1 and 3650")
    as_of = as_of.astimezone(UTC)
    window_start = as_of - timedelta(days=window_days)

    bindings = [
        row
        for row in session.scalars(select(CloudTenantBinding))
        if window_start <= _utc(row.created_at) <= as_of
    ]
    cohort = {row.local_tenant_id: _utc(row.created_at) for row in bindings}
    tenant_ids = set(cohort)

    backtests = [
        row
        for row in session.scalars(select(EconomicBacktestRecord))
        if row.tenant_id in tenant_ids and _utc(row.evaluated_at) <= as_of
    ]
    subscriptions = [
        row
        for row in session.scalars(select(CloudSubscription))
        if row.tenant_id in tenant_ids
    ]
    by_tenant: dict[str, list[EconomicBacktestRecord]] = defaultdict(list)
    for row in backtests:
        by_tenant[row.tenant_id].append(row)

    first_value_tenants = set(by_tenant)
    repeat_value_tenants = {
        tenant_id for tenant_id, rows in by_tenant.items() if len(rows) >= 2
    }
    checkout_tenants = {
        row.tenant_id for row in subscriptions if row.external_customer_id is not None
    }
    paid_tenants = {
        row.tenant_id
        for row in subscriptions
        if row.plan == "solo" and row.status == "active"
    }
    canceled_tenants = {row.tenant_id for row in subscriptions if row.status == "canceled"}
    past_due_tenants = {row.tenant_id for row in subscriptions if row.status == "past_due"}

    activation_lags = []
    negative_activation_lags = 0
    for tenant_id, rows in by_tenant.items():
        first = min(_utc(row.evaluated_at) for row in rows)
        lag = (first - cohort[tenant_id]).total_seconds() / 3600
        if lag < 0:
            negative_activation_lags += 1
        else:
            activation_lags.append(lag)

    signed_up = len(tenant_ids)
    first_value = len(first_value_tenants)
    paid_active = len(paid_tenants)
    median = round(statistics.median(activation_lags), 2) if activation_lags else None
    return {
        "schema_version": 1,
        "as_of": as_of.isoformat().replace("+00:00", "Z"),
        "window_days": window_days,
        "window_start": window_start.isoformat().replace("+00:00", "Z"),
        "stages": {
            "signed_up": signed_up,
            "first_value": first_value,
            "repeat_value": len(repeat_value_tenants),
            "checkout_completed": len(checkout_tenants),
            "paid_active": paid_active,
            "canceled": len(canceled_tenants),
            "past_due": len(past_due_tenants),
        },
        "conversion": {
            "signup_to_first_value_pct": _rate(first_value, signed_up),
            "signup_to_checkout_completed_pct": _rate(len(checkout_tenants), signed_up),
            "signup_to_paid_active_pct": _rate(paid_active, signed_up),
            "first_value_to_paid_active_pct": _rate(paid_active, first_value),
        },
        "time_to_first_value_hours": {
            "median": median,
            "p90": _percentile_nearest_rank(activation_lags, 0.9),
        },
        "backtest_verdicts": dict(sorted(Counter(row.verdict for row in backtests).items())),
        "subscription_statuses": dict(
            sorted(Counter(row.status for row in subscriptions).items())
        ),
        "data_quality": {"negative_activation_lags": negative_activation_lags},
        "privacy": "aggregate counts only; no tenant, identity, customer, or payload fields",
        "limitations": [
            "subscription states are current projections, not reconstructed history",
            "checkout_completed means Paddle subscription ownership was projected locally",
        ],
    }


def _session() -> Session:
    from zeroth.econ.plane.database import SessionLocal

    return SessionLocal()


def _now() -> datetime:
    return datetime.now(UTC)


def _write_report_atomic(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Write an aggregate, identifier-free Zeroth Cloud funnel report."
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--window-days", type=int, default=30)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        with _session() as session:
            report = build_funnel_report(
                session,
                as_of=_now(),
                window_days=args.window_days,
            )
    except (ValueError, OSError):
        print("cloud funnel report failed: invalid input or unavailable database", file=sys.stderr)
        return 2
    _write_report_atomic(args.output, report)
    print(f"cloud funnel report written: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
