"""Independent ledger reader: totals from stored rows only, in 1e-8 USD units.

This is the oracle every recipe is compared against. It never sees the
workload or the rate card; it reads what the economic plane retained.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from zeroth.econ.plane.instrumentation.models import ExecutionEvent, OutcomeEvent

UNITS = Decimal(100_000_000)


def _units(amount: Decimal | None) -> int | None:
    if amount is None:
        return None
    scaled = Decimal(amount) * UNITS
    assert scaled == scaled.to_integral_value(), f"{amount} exceeds 8 decimals"
    return int(scaled)


def read(engine, workflow: str) -> dict:
    """Per-version, per-run totals in the shape of ``expected_ledger.json``."""
    versions: dict[str, dict] = defaultdict(lambda: {"runs": {}})
    with Session(engine) as db:
        executions = db.scalars(
            select(ExecutionEvent).where(ExecutionEvent.workflow_id == workflow)
        ).all()
        outcomes = db.scalars(
            select(OutcomeEvent).where(OutcomeEvent.workflow_id == workflow)
        ).all()
    for row in executions:
        run = versions[row.workflow_version]["runs"].setdefault(
            row.run_id or row.join_key, _empty_run()
        )
        if row.cost_role == "summary":
            run["summaries"] += 1
            assert row.token_cost_usd is None and row.tool_cost_usd is None
            continue
        assert row.cost_role == "charge", row.cost_role
        run["charges"] += 1
        kind = row.event_metadata["charge_kind"]
        amount = _units(row.token_cost_usd)
        if row.cost_measurement == "unmeasured" or amount is None:
            assert amount is None and row.cost_measurement == "unmeasured"
            run["unmeasured_charges"] += 1
            continue
        run[f"{kind}_cost_units"] += amount
        run["measured_cost_units"] += amount
    for row in outcomes:
        run = versions[row.workflow_version]["runs"].setdefault(row.join_key, _empty_run())
        run["outcomes"] += 1
        latest = run.get("_latest")
        if latest is None or row.occurred_at > latest:
            run["_latest"] = row.occurred_at
            run["accepted"] = row.outcome_payload_json.get("accepted")
            run["maturity"] = row.maturity
    result = {}
    for version, data in sorted(versions.items()):
        runs = {}
        for run_id, run in sorted(data["runs"].items()):
            run.pop("_latest", None)
            runs[run_id] = run
        result[version] = {
            "runs": runs,
            "totals": {
                "runs": len(runs),
                "charges": sum(r["charges"] for r in runs.values()),
                "unmeasured_charges": sum(r["unmeasured_charges"] for r in runs.values()),
                "measured_cost_units": sum(r["measured_cost_units"] for r in runs.values()),
                "accepted_runs": sum(1 for r in runs.values() if r["accepted"] is True),
            },
        }
    return result


def _empty_run() -> dict:
    return {
        "charges": 0, "unmeasured_charges": 0, "measured_cost_units": 0,
        "model_cost_units": 0, "tool_cost_units": 0, "summaries": 0,
        "outcomes": 0, "accepted": None, "maturity": None,
    }
